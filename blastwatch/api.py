"""REST API and static dashboard."""
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config
from .db import SessionLocal
from .models import District, Gene, GeneRecord, Production, RiskDaily, WeatherHourly
from .pipeline import advice_for, merged_hours
from .risk import load_rules

INDIA_AREA_CODE = 100


def create_app(session_factory: Callable[[], Session] = SessionLocal, scheduler: bool = False) -> FastAPI:
    rules = load_rules()
    model_version = rules["model_version"]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sched = None
        if scheduler:
            from .scheduler import start_background

            sched = start_background(session_factory)
        yield
        if sched:
            sched.shutdown(wait=False)

    app = FastAPI(title="BlastWatch API", version="0.1.0", lifespan=lifespan)

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
        return {
            "status": "ok",
            "model_version": model_version,
            "districts": session.scalar(select(func.count(District.id))),
            "last_weather_fetch": session.scalar(select(func.max(WeatherHourly.fetched_at))),
            "last_risk_compute": session.scalar(select(func.max(RiskDaily.computed_at))),
        }

    @app.get("/api/districts")
    def districts(session: Session = Depends(get_session)):
        return [
            {"id": d.id, "name": d.name, "state": d.state, "lat": d.lat, "lon": d.lon,
             "rice_area_ha": d.rice_area_ha}
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
                 "mean_temp_c": r.mean_temp_c, "is_forecast": r.is_forecast}
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
                 "cloud_pct": h.cloud_pct, "is_forecast": h.is_forecast}
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
