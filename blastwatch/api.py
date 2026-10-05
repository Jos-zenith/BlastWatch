"""REST API and static dashboard.

The API only ever reads the local database; third-party weather APIs are called by the
background scheduler, never on a page request, so a slow or failing provider cannot slow the
dashboard. Every response that depends on the forecast carries its freshness status.
"""
import hmac
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config
from .db import SessionLocal, init_db, upsert
from .ingest.weather import IST
from .models import Alert, District, Gene, GeneRecord, IngestRun, Production, RiskDaily, WeatherHourly
from .pipeline import advice_for, compute_risk, load_seeds, merged_hours
from .risk import load_rules
from .runs import freshness

INDIA_AREA_CODE = 100
STATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,12}$")


class SensorReading(BaseModel):
    ts: datetime
    temp_c: float | None = Field(None, ge=-10, le=55)
    rh_pct: float | None = Field(None, ge=0, le=100)
    leaf_wet_min: float | None = Field(None, ge=0, le=60)
    precip_mm: float | None = Field(None, ge=0, le=300)


class SensorBatch(BaseModel):
    station_id: str
    district: str
    readings: list[SensorReading] = Field(..., min_length=1, max_length=2000)

    @field_validator("station_id")
    @classmethod
    def _station(cls, v: str) -> str:
        if not STATION_RE.match(v):
            raise ValueError("station_id must be 1-12 chars of letters, digits, '-' or '_'")
        return v


def to_local_hour(ts: datetime) -> datetime:
    """Sensor timestamps: aware -> IST; naive is assumed IST. Floored to the hour."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(IST).replace(tzinfo=None)
    return ts.replace(minute=0, second=0, microsecond=0)


def create_app(session_factory: Callable[[], Session] = SessionLocal, scheduler: bool = False) -> FastAPI:
    rules = load_rules()
    model_version = rules["model_version"]
    alert_rules = rules["alerts"]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Create/upgrade tables and sync seed CSVs (idempotent upserts), so a fresh or
        # existing deployment always matches the code it runs.
        init_db(session_factory.kw["bind"])
        with session_factory() as s:
            load_seeds(s)
        sched = None
        if scheduler:
            from .scheduler import start_background

            sched = start_background(session_factory)
        yield
        if sched:
            sched.shutdown(wait=False)

    app = FastAPI(title="BlastWatch API", version="0.2.0", lifespan=lifespan)

    def get_session():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def get_district(district_id: int, session: Session) -> District:
        district = session.get(District, district_id)
        if district is None:
            raise HTTPException(404, "district not found")
        return district

    def latest_india_yield(session: Session) -> float | None:
        return session.scalar(
            select(Production.yield_kg_ha)
            .where(Production.area_code == INDIA_AREA_CODE, Production.yield_kg_ha.is_not(None))
            .order_by(Production.year.desc())
            .limit(1)
        )

    @app.get("/api/health")
    def health(session: Session = Depends(get_session)):
        sources = {}
        for source, status, last in session.execute(
            select(IngestRun.source, IngestRun.status, func.max(IngestRun.finished_at))
            .group_by(IngestRun.source, IngestRun.status)
        ):
            sources.setdefault(source, {})[f"last_{status}"] = last
        return {
            "status": "ok",
            "model_version": model_version,
            "districts": session.scalar(select(func.count(District.id))),
            "forecast": freshness(session, rules),
            "sources": sources,
            "last_risk_compute": session.scalar(select(func.max(RiskDaily.computed_at))),
        }

    @app.get("/api/outlook")
    def outlook(session: Session = Depends(get_session)):
        """Officer view: what to do in each district, most urgent first."""
        today = date.today()
        fresh = freshness(session, rules)
        horizon = alert_rules["horizon_days"]
        rows = session.scalars(
            select(RiskDaily).where(RiskDaily.model_version == model_version, RiskDaily.date >= today,
                                    RiskDaily.date < today + timedelta(days=7))
            .order_by(RiskDaily.date)
        ).all()
        by_district: dict[int, list[RiskDaily]] = {}
        for r in rows:
            by_district.setdefault(r.district_id, []).append(r)
        open_alerts = {
            a.district_id: a for a in session.scalars(
                select(Alert).where(Alert.episode_start >= today - timedelta(days=alert_rules["cooldown_days"]))
            )
        }

        result = []
        for d in session.scalars(select(District)).all():
            days = by_district.get(d.id, [])
            near = [r for r in days if (r.date - today).days < horizon]
            near_high = sum(r.level == "High" for r in near)
            if fresh["status"] == "expired" or not days:
                action = "unknown"
            elif near_high >= alert_rules["min_high_days"]:
                action = "alert"
            elif any(r.level != "Low" for r in near) or any(r.level == "High" for r in days):
                action = "watch"
            else:
                action = "none"
            alert = open_alerts.get(d.id)
            result.append({
                "id": d.id, "name": d.name, "name_ta": d.name_ta, "lat": d.lat, "lon": d.lon,
                "action": action,
                "peak_near_score": max((r.score for r in near), default=None),
                "days": [
                    {"date": r.date, "level": r.level, "score": r.score,
                     "horizon": "near" if (r.date - today).days < horizon else "outlook",
                     "leaf_wet_hours": r.wet_hours, "longest_wet_run": r.longest_wet_run,
                     "rain_mm": r.rain_mm, "wetness_basis": r.wetness_basis}
                    for r in days
                ],
                "alert": None if alert is None else {"id": alert.id, "status": alert.status},
            })
        order = {"alert": 0, "watch": 1, "none": 2, "unknown": 3}
        result.sort(key=lambda x: (order[x["action"]], -(x["peak_near_score"] or 0), x["name"]))
        return {"date": today, "model_version": model_version, "freshness": fresh,
                "alert_rule": alert_rules, "districts": result}

    @app.get("/api/alerts")
    def list_alerts(days: int = 14, session: Session = Depends(get_session)):
        since = datetime.now() - timedelta(days=days)
        rows = session.execute(
            select(Alert, District).join(District, District.id == Alert.district_id)
            .where(Alert.created_at >= since).order_by(Alert.created_at.desc())
        ).all()
        return [
            {"id": a.id, "district": d.name, "district_ta": d.name_ta, "episode_start": a.episode_start,
             "high_days": a.high_days.split(";"), "message_en": a.message_en, "message_ta": a.message_ta,
             "status": a.status, "created_at": a.created_at, "sent_at": a.sent_at}
            for a, d in rows
        ]

    @app.post("/api/alerts/{alert_id}/sent")
    def mark_sent(alert_id: int, session: Session = Depends(get_session)):
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise HTTPException(404, "alert not found")
        alert.status, alert.sent_at = "sent", datetime.now()
        session.commit()
        return {"id": alert.id, "status": alert.status, "sent_at": alert.sent_at}

    @app.post("/api/sensors/readings", status_code=202)
    def sensor_readings(batch: SensorBatch, x_api_key: str = Header(""), session: Session = Depends(get_session)):
        """Canopy-level readings from field stations; they override grid weather for that hour."""
        if not config.INGEST_KEY:
            raise HTTPException(503, "sensor ingest disabled: set BLASTWATCH_INGEST_KEY")
        if not hmac.compare_digest(x_api_key, config.INGEST_KEY):
            raise HTTPException(401, "invalid API key")
        district = session.scalar(select(District).where(func.lower(District.name) == batch.district.lower()))
        if district is None:
            raise HTTPException(404, "district not found")
        now = datetime.now()
        rows = {}
        for r in batch.readings:
            ts = to_local_hour(r.ts)
            rows[ts] = {"district_id": district.id, "ts": ts, "source": f"sensor:{batch.station_id}",
                        "is_forecast": False, "fetched_at": now, "temp_c": r.temp_c, "rh_pct": r.rh_pct,
                        "precip_mm": r.precip_mm, "cloud_pct": None, "dew_point_c": None,
                        "leaf_wet_prob": None, "leaf_wet_min": r.leaf_wet_min}
        upsert(session, WeatherHourly, list(rows.values()), ["district_id", "ts", "source"])
        session.commit()
        compute_risk(session, rules)
        return {"accepted": len(rows), "district": district.name}

    @app.get("/api/districts")
    def districts(session: Session = Depends(get_session)):
        return [
            {"id": d.id, "name": d.name, "name_ta": d.name_ta, "state": d.state, "lat": d.lat,
             "lon": d.lon, "rice_area_ha": d.rice_area_ha}
            for d in session.scalars(select(District).order_by(District.name))
        ]

    @app.get("/api/risk")
    def risk_map(day: date | None = Query(None, alias="date"), session: Session = Depends(get_session)):
        dates = session.scalars(
            select(RiskDaily.date).where(RiskDaily.model_version == model_version)
            .distinct().order_by(RiskDaily.date)
        ).all()
        if not dates:
            return {"date": None, "available_dates": [], "model_version": model_version, "districts": []}
        if day is None:
            today = date.today()
            day = today if today in dates else min(dates, key=lambda d: abs((d - today).days))

        india_yield = latest_india_yield(session)
        rows = session.execute(
            select(District, RiskDaily)
            .join(RiskDaily, (RiskDaily.district_id == District.id)
                  & (RiskDaily.date == day) & (RiskDaily.model_version == model_version))
            .order_by(RiskDaily.score.desc())
        ).all()
        result = []
        for d, r in rows:
            exposed_t = None
            if d.rice_area_ha and india_yield and r.level == "High":
                exposed_t = round(d.rice_area_ha * india_yield / 1000)
            result.append({
                "id": d.id, "name": d.name, "state": d.state, "lat": d.lat, "lon": d.lon,
                "score": r.score, "level": r.level, "wet_hours": r.wet_hours,
                "longest_wet_run": r.longest_wet_run, "rain_mm": r.rain_mm,
                "mean_temp_c": r.mean_temp_c, "is_forecast": r.is_forecast,
                "wetness_basis": r.wetness_basis,
                "rice_area_ha": d.rice_area_ha, "production_exposed_t": exposed_t,
            })
        return {"date": day, "available_dates": dates, "model_version": model_version, "districts": result}

    @app.get("/api/districts/{district_id}/risk")
    def district_risk(district_id: int, days_back: int = 7, session: Session = Depends(get_session)):
        district = get_district(district_id, session)
        since = date.today() - timedelta(days=days_back)
        rows = session.scalars(
            select(RiskDaily)
            .where(RiskDaily.district_id == district.id, RiskDaily.model_version == model_version,
                   RiskDaily.date >= since)
            .order_by(RiskDaily.date)
        ).all()
        return {
            "district": district.name,
            "series": [
                {"date": r.date, "score": r.score, "level": r.level, "wet_hours": r.wet_hours,
                 "longest_wet_run": r.longest_wet_run, "rain_mm": r.rain_mm,
                 "mean_temp_c": r.mean_temp_c, "is_forecast": r.is_forecast,
                 "wetness_basis": r.wetness_basis}
                for r in rows
            ],
        }

    @app.get("/api/districts/{district_id}/weather")
    def district_weather(district_id: int, hours_back: int = 48, session: Session = Depends(get_session)):
        district = get_district(district_id, session)
        since = datetime.now() - timedelta(hours=hours_back)
        return {
            "district": district.name,
            "timezone": config.TIMEZONE,
            "hours": [
                {"ts": h.ts, "temp_c": h.temp_c, "rh_pct": h.rh_pct, "precip_mm": h.precip_mm,
                 "cloud_pct": h.cloud_pct, "dew_point_c": h.dew_point_c,
                 "leaf_wet_prob": h.leaf_wet_prob, "leaf_wet_min": h.leaf_wet_min,
                 "is_forecast": h.is_forecast}
                for h in merged_hours(session, district.id) if h.ts >= since
            ],
        }

    @app.get("/api/districts/{district_id}/advice")
    def district_advice(district_id: int, session: Session = Depends(get_session)):
        district = get_district(district_id, session)
        # Advice follows the worst level expected over the next five days.
        upcoming = session.scalars(
            select(RiskDaily.level).where(
                RiskDaily.district_id == district.id, RiskDaily.model_version == model_version,
                RiskDaily.date >= date.today(), RiskDaily.date <= date.today() + timedelta(days=5),
            )
        ).all()
        worst = next((lvl for lvl in ("High", "Moderate", "Low") if lvl in upcoming), None)
        return {"district": district.name, "state": district.state, **advice_for(session, district, worst)}

    @app.get("/api/production/areas")
    def production_areas(session: Session = Depends(get_session)):
        return session.scalars(select(Production.area).distinct().order_by(Production.area)).all()

    @app.get("/api/production")
    def production(
        areas: list[str] = Query(["India"]),
        start: int = 1961,
        session: Session = Depends(get_session),
    ):
        rows = session.scalars(
            select(Production).where(Production.area.in_(areas), Production.year >= start)
            .order_by(Production.area, Production.year)
        ).all()
        series: dict[str, list] = {a: [] for a in areas}
        for r in rows:
            series[r.area].append({"year": r.year, "area_ha": r.area_ha,
                                   "production_t": r.production_t, "yield_kg_ha": r.yield_kg_ha})
        return {"item": "Rice", "source": "FAOSTAT (QCL)", "series": series}

    @app.get("/api/genes")
    def genes(session: Session = Depends(get_session)):
        counts = dict(session.execute(
            select(GeneRecord.gene_id, func.count(GeneRecord.id)).group_by(GeneRecord.gene_id)
        ).all())
        return [
            {"symbol": g.symbol, "organism": g.organism, "role": g.role, "description": g.description,
             "ncbi_hits": g.ncbi_hits, "records_cached": counts.get(g.id, 0), "fetched_at": g.fetched_at}
            for g in session.scalars(select(Gene).order_by(Gene.role, Gene.symbol))
        ]

    @app.get("/api/genes/{symbol}")
    def gene_detail(symbol: str, session: Session = Depends(get_session)):
        gene = session.scalar(select(Gene).where(func.lower(Gene.symbol) == symbol.lower()))
        if gene is None:
            raise HTTPException(404, "gene not found")
        records = session.scalars(select(GeneRecord).where(GeneRecord.gene_id == gene.id)).all()
        return {
            "symbol": gene.symbol, "organism": gene.organism, "role": gene.role,
            "description": gene.description, "ncbi_hits": gene.ncbi_hits,
            "records": [
                {"accession": r.accession, "title": r.title, "length": r.length,
                 "organism": r.organism, "update_date": r.update_date,
                 "url": f"https://www.ncbi.nlm.nih.gov/nuccore/{r.accession}"}
                for r in records
            ],
        }

    if config.WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
    return app
