"""REST API and static dashboard.

The API only ever reads the local database; third-party weather APIs are called by the
background scheduler, never on a page request, so a slow or failing provider cannot slow the
dashboard. Every response that depends on the forecast carries its freshness status.

Farmer endpoints (enrolment, replies, outbox, report verification) need the X-Officer-Key
header and are disabled while BLASTWATCH_OFFICER_KEY is unset: they hold contact details,
and the replies they record become calibration data.
"""
import asyncio
import hmac
import re
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import alerts as alerts_mod
from . import blocks, config, ensemble, farmers
from .db import SessionLocal, init_db
from .events import bus, sse
from .ingest.weather import IST
from .models import (Alert, Block, BlockRiskDaily, District, EnsembleDaily, FarmerMessage, FieldCheck, Followup, Gene, GeneRecord, IngestRun,
                     Observation, Production, RiskDaily, Subscriber)
from .pipeline import advice_for, load_seeds, merged_hours
from .risk import load_rules, score_points
from .runs import freshness
from .sensors import ingest_readings
from .sim import SCENARIOS, VirtualStation
from .stage import load_crop_calendar

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


class Enrolment(BaseModel):
    contact: str = Field(..., min_length=3, max_length=64)
    channel: Literal["outbox", "telegram"] = "outbox"
    district: str
    block: str | None = Field(None, max_length=80)
    variety: str = Field(..., min_length=1, max_length=80)
    method: Literal["transplanted", "direct_seeded"]
    establish_date: date
    language: Literal["ta", "en"] = "ta"
    consent: bool


class ContactRef(BaseModel):
    contact: str


class Reply(BaseModel):
    contact: str
    answer: Literal["yes", "no", "unsure"]


class SimStart(BaseModel):
    district_id: int
    scenario: Literal["dew", "showers", "dry"] = "dew"
    seconds_per_hour: float = Field(1.5, ge=0.2, le=10)


class Verification(BaseModel):
    status: Literal["confirmed", "rejected"]
    note: str = Field("", max_length=1000)


class FieldCheckIn(BaseModel):
    blast_found: bool
    alert_id: int | None = None
    checked_on: date | None = None  # defaults to today
    fields_checked: int | None = Field(None, ge=1, le=500)
    block: str | None = Field(None, max_length=80)
    note: str = Field("", max_length=1000)


def officer(x_officer_key: str = Header("")) -> None:
    if not config.OFFICER_KEY:
        raise HTTPException(503, "farmer endpoints disabled: set BLASTWATCH_OFFICER_KEY")
    if not hmac.compare_digest(x_officer_key, config.OFFICER_KEY):
        raise HTTPException(401, "invalid officer key")


def demo() -> None:
    if not config.DEMO:
        raise HTTPException(503, "demo controls disabled: set BLASTWATCH_DEMO=1")


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
        app.state.scheduler = sched
        yield
        if sched:
            sched.shutdown(wait=False)

    app = FastAPI(title="BlastWatch API", version="0.4.0", lifespan=lifespan)
    app.state.scheduler = None
    station = VirtualStation(session_factory)

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
        calendar = load_crop_calendar()
        no_farmers = {"subscribers": 0, "susceptible": 0, "unverified_reports": 0}
        farmer_counts: dict[int, dict] = {}
        for sub in session.scalars(select(Subscriber).where(Subscriber.active)):
            c = farmer_counts.setdefault(sub.district_id, dict(no_farmers))
            c["subscribers"] += 1
            c["susceptible"] += farmers.stage_of(sub, today, rules, calendar).susceptible
        for district_id, n in session.execute(
            select(Subscriber.district_id, func.count(Followup.id))
            .join(Followup, Followup.subscriber_id == Subscriber.id)
            .where(Followup.answer.in_(("yes", "unsure")), Followup.officer_status == "unverified")
            .group_by(Subscriber.district_id)
        ):
            farmer_counts.setdefault(district_id, dict(no_farmers))["unverified_reports"] = n
        open_alerts = {
            a.district_id: a for a in session.scalars(
                select(Alert).where(Alert.episode_start >= today - timedelta(days=alert_rules["cooldown_days"]))
                .order_by(Alert.episode_start)
            )
        }
        last_checks = {
            c.district_id: c for c in session.scalars(select(FieldCheck).order_by(FieldCheck.checked_on, FieldCheck.id))
        }
        pooled = {
            (e.district_id, e.date): e for e in session.scalars(
                select(EnsembleDaily).where(EnsembleDaily.model == ensemble.POOLED,
                                            EnsembleDaily.model_version == model_version, EnsembleDaily.date >= today))
        }
        block_roll = blocks.rollup(session, rules, today)

        share = alert_rules.get("block_share", 0.5)
        blocks_fresh = blocks.fresh(session, rules)

        def decide(levels: list[tuple[date, str]]) -> str:
            near = [lv for day, lv in levels if (day - today).days < horizon]
            if fresh["status"] == "expired" or not levels:
                return "unknown"
            if sum(lv == "High" for lv in near) >= alert_rules["min_high_days"]:
                return "alert"
            if any(lv != "Low" for lv in near) or any(lv == "High" for _, lv in levels):
                return "watch"
            return "none"

        configured = alert_rules.get("district_rule", "hq")
        result = []
        for d in session.scalars(select(District)).all():
            days = by_district.get(d.id, [])
            near = [r for r in days if (r.date - today).days < horizon]
            by_blocks = [(r.date, blocks.share_level(block_roll[(d.id, r.date)], share)) for r in days
                         if (d.id, r.date) in block_roll] if blocks_fresh else []
            rule_actions = {"hq": decide([(r.date, r.level) for r in days]),
                            "blocks_half": decide(by_blocks) if by_blocks else None}
            rule_used = "blocks_half" if configured == "blocks_half" and by_blocks else "hq"
            action = rule_actions[rule_used]
            alert = open_alerts.get(d.id)
            result.append({
                "id": d.id, "name": d.name, "name_ta": d.name_ta, "lat": d.lat, "lon": d.lon,
                "action": action, "rule_used": rule_used, "rule_actions": rule_actions,
                "peak_near_score": max((r.score for r in near), default=None),
                "days": [
                    {"date": r.date, "level": r.level, "score": r.score,
                     "horizon": "near" if (r.date - today).days < horizon else "outlook",
                     "leaf_wet_hours": r.wet_hours, "longest_wet_run": r.longest_wet_run,
                     "rain_mm": r.rain_mm, "mean_cloud_pct": r.mean_cloud_pct, "mean_temp_c": r.mean_temp_c,
                     "susceptibility": r.susceptibility, "wetness_basis": r.wetness_basis,
                     "points": score_points(r.longest_wet_run, r.wet_hours, r.rain_mm, r.mean_cloud_pct, rules),
                     "p_high": pooled[(d.id, r.date)].p_high if (d.id, r.date) in pooled else None,
                     "ens_members": pooled[(d.id, r.date)].members if (d.id, r.date) in pooled else None,
                     "blocks": block_roll.get((d.id, r.date)),
                     "level_blocks": blocks.share_level(block_roll[(d.id, r.date)], share)
                     if (d.id, r.date) in block_roll else None}
                    for r in days
                ],
                "alert": None if alert is None else {"id": alert.id, "status": alert.status},
                "last_check": None if d.id not in last_checks else {
                    "checked_on": last_checks[d.id].checked_on, "blast_found": last_checks[d.id].blast_found},
                "farmers": farmer_counts.get(d.id, no_farmers),
            })
        order = {"alert": 0, "watch": 1, "none": 2, "unknown": 3}
        result.sort(key=lambda x: (order[x["action"]], -(x["peak_near_score"] or 0), x["name"]))
        return {"date": today, "model_version": model_version, "freshness": fresh,
                "alert_rule": alert_rules, "levels": rules["levels"], "district_rule": configured,
                "blocks_fresh": blocks_fresh, "districts": result}

    @app.get("/api/alerts")
    def list_alerts(days: int = 14, session: Session = Depends(get_session)):
        since = datetime.now() - timedelta(days=days)
        rows = session.execute(
            select(Alert, District).join(District, District.id == Alert.district_id)
            .where(Alert.created_at >= since).order_by(Alert.created_at.desc())
        ).all()
        checks: dict[int, list[dict]] = {}
        for c in session.scalars(select(FieldCheck).where(FieldCheck.alert_id.in_([a.id for a, _ in rows]))
                                 .order_by(FieldCheck.checked_on)):
            checks.setdefault(c.alert_id, []).append({"checked_on": c.checked_on, "blast_found": c.blast_found})
        return [
            {"id": a.id, "district": d.name, "district_ta": d.name_ta, "episode_start": a.episode_start,
             "high_days": a.high_days.split(";"), "message_en": a.message_en, "message_ta": a.message_ta,
             "status": a.status, "created_at": a.created_at, "generated_at": a.updated_at or a.created_at,
             "sent_at": a.sent_at, "field_checks": checks.get(a.id, [])}
            for a, d in rows
        ]

    @app.post("/api/alerts/{alert_id}/sent")
    def mark_sent(alert_id: int, session: Session = Depends(get_session)):
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise HTTPException(404, "alert not found")
        if alert.status == "expired":
            raise HTTPException(409, "this message is out of date; its days have passed")
        alert.status, alert.sent_at = "sent", datetime.now()
        session.commit()
        return {"id": alert.id, "status": alert.status, "sent_at": alert.sent_at}

    @app.post("/api/districts/{district_id}/field-checks", status_code=201)
    def add_field_check(district_id: int, c: FieldCheckIn, session: Session = Depends(get_session)):
        """One tap after a field visit. It becomes an Observation (present or absent) for calibration.
        Routine visits without an alert count too: they are what shows outbreaks the model missed."""
        district = get_district(district_id, session)
        today = date.today()
        checked_on = c.checked_on or today
        if checked_on > today or (today - checked_on).days > 30:
            raise HTTPException(400, "checked_on must be within the last 30 days")
        if c.alert_id is not None:
            alert = session.get(Alert, c.alert_id)
            if alert is None or alert.district_id != district.id:
                raise HTTPException(404, "alert not found for this district")
        check = FieldCheck(district_id=district.id, alert_id=c.alert_id, checked_on=checked_on,
                           blast_found=c.blast_found, fields_checked=c.fields_checked, block=c.block,
                           note=c.note or None, created_at=datetime.now())
        session.add(check)
        session.flush()
        source = f"field-check:{check.id}"
        session.add(Observation(district_id=district.id, date=checked_on, blast_present=c.blast_found,
                                source=source, note=c.note or None))
        session.commit()
        return {"id": check.id, "observation": source}

    @app.get("/api/districts/{district_id}/field-checks")
    def field_checks(district_id: int, days: int = 60, session: Session = Depends(get_session)):
        district = get_district(district_id, session)
        rows = session.scalars(
            select(FieldCheck).where(FieldCheck.district_id == district.id,
                                     FieldCheck.checked_on >= date.today() - timedelta(days=days))
            .order_by(FieldCheck.checked_on.desc(), FieldCheck.id.desc())
        ).all()
        return [{"id": c.id, "alert_id": c.alert_id, "checked_on": c.checked_on, "blast_found": c.blast_found,
                 "fields_checked": c.fields_checked, "block": c.block, "note": c.note} for c in rows]

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
        readings = [{**r.model_dump(), "ts": to_local_hour(r.ts)} for r in batch.readings]
        result = ingest_readings(session, district, batch.station_id, readings, rules)
        return {"accepted": result["accepted"], "district": district.name, "day": result["day"],
                "changes": result["changes"]}

    @app.get("/api/consent")
    def consent():
        """Read to the farmer, in their language, before enrolment."""
        return {"version": farmers.CONSENT_VERSION, "text": farmers.CONSENT_TEXT}

    @app.post("/api/subscribers", dependencies=[Depends(officer)])
    def enrol(e: Enrolment, session: Session = Depends(get_session)):
        """Enrol a farmer, or update an existing subscriber's crop for a new season."""
        if not e.consent:
            raise HTTPException(400, "consent required")
        district = session.scalar(select(District).where(func.lower(District.name) == e.district.lower()))
        if district is None:
            raise HTTPException(404, "district not found")
        today = date.today()
        if e.establish_date > today:
            raise HTTPException(400, "establish_date is in the future")
        if (today - e.establish_date).days > 200:
            raise HTTPException(400, "establish_date is too old for a current crop")
        now = datetime.now()
        sub = session.scalar(select(Subscriber).where(Subscriber.contact == e.contact))
        created = sub is None
        if created:
            sub = Subscriber(contact=e.contact, created_at=now)
            session.add(sub)
        sub.channel, sub.district_id, sub.block, sub.variety = e.channel, district.id, e.block, e.variety
        sub.method, sub.establish_date, sub.language = e.method, e.establish_date, e.language
        sub.consent_at, sub.consent_version, sub.active = now, farmers.CONSENT_VERSION, True
        session.commit()
        stage = farmers.stage_of(sub, today, rules, load_crop_calendar())
        return {"id": sub.id, "created": created, "stage": stage.name, "flags": list(stage.flags)}

    @app.post("/api/subscribers/stop", dependencies=[Depends(officer)])
    def stop(ref: ContactRef, session: Session = Depends(get_session)):
        sub = session.scalar(select(Subscriber).where(Subscriber.contact == ref.contact))
        if sub is None:
            raise HTTPException(404, "subscriber not found")
        sub.active = False
        session.commit()
        return {"id": sub.id, "active": False}

    @app.get("/api/subscribers", dependencies=[Depends(officer)])
    def subscribers(session: Session = Depends(get_session)):
        today, calendar = date.today(), load_crop_calendar()
        result = []
        for sub, district in session.execute(
            select(Subscriber, District).join(District, District.id == Subscriber.district_id)
            .order_by(District.name, Subscriber.id)
        ):
            stage = farmers.stage_of(sub, today, rules, calendar)
            result.append({"id": sub.id, "contact": sub.contact, "channel": sub.channel,
                           "district": district.name, "block": sub.block, "variety": sub.variety,
                           "method": sub.method, "establish_date": sub.establish_date,
                           "language": sub.language, "active": sub.active, "stage": stage.name,
                           "age_days": stage.age_days, "flags": list(stage.flags)})
        return result

    @app.post("/api/replies", dependencies=[Depends(officer)])
    def reply(r: Reply, session: Session = Depends(get_session)):
        followup = farmers.record_reply(session, r.contact, r.answer)
        if followup is None:
            raise HTTPException(404, "no open question for this contact")
        return {"followup_id": followup.id, "answer": followup.answer,
                "note": "A YES is a report until an officer verifies it."}

    @app.get("/api/farmer-messages", dependencies=[Depends(officer)])
    def farmer_messages(status: str = "queued", session: Session = Depends(get_session)):
        """The outbox: messages waiting for an officer to forward them."""
        rows = session.execute(
            select(FarmerMessage, Subscriber).join(Subscriber, Subscriber.id == FarmerMessage.subscriber_id)
            .where(FarmerMessage.status == status).order_by(FarmerMessage.created_at)
        ).all()
        return [{"id": m.id, "contact": s.contact, "language": s.language, "kind": m.kind, "stage": m.stage,
                 "body": m.body, "status": m.status, "created_at": m.created_at} for m, s in rows]

    @app.post("/api/farmer-messages/{message_id}/sent", dependencies=[Depends(officer)])
    def farmer_message_sent(message_id: int, session: Session = Depends(get_session)):
        msg = session.get(FarmerMessage, message_id)
        if msg is None:
            raise HTTPException(404, "message not found")
        msg.status, msg.sent_at = "sent", datetime.now()
        session.commit()
        return {"id": msg.id, "status": msg.status, "sent_at": msg.sent_at}

    @app.get("/api/reports", dependencies=[Depends(officer)])
    def reports(session: Session = Depends(get_session)):
        """Farmer YES / NOT SURE answers waiting for an officer to check the field."""
        rows = session.execute(
            select(Followup, Subscriber, District)
            .join(Subscriber, Subscriber.id == Followup.subscriber_id)
            .join(District, District.id == Subscriber.district_id)
            .where(Followup.answer.in_(("yes", "unsure")), Followup.officer_status == "unverified")
            .order_by(Followup.answered_at)
        ).all()
        return [{"id": f.id, "district": d.name, "block": s.block, "contact": s.contact, "variety": s.variety,
                 "origin": f.origin, "answer": f.answer, "answered_at": f.answered_at} for f, s, d in rows]

    @app.post("/api/reports/{followup_id}/verify", dependencies=[Depends(officer)])
    def verify(followup_id: int, v: Verification, session: Session = Depends(get_session)):
        try:
            f = farmers.verify_report(session, followup_id, v.status, v.note)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"id": f.id, "officer_status": f.officer_status, "observation": f"farmer-report:{f.id}"}

    # ---- Live updates -------------------------------------------------------------------

    @app.get("/api/stream")
    async def stream(request: Request, last_event_id: str = Header("")):
        """Server-Sent Events: every refresh step, sensor reading, risk change and alert, as it happens.
        A reconnecting browser sends Last-Event-ID and receives what it missed from the recent history."""
        loop, queue = bus.subscribe()

        async def events():
            try:
                yield "retry: 3000\n\n"
                if last_event_id.isdigit():
                    for event in bus.since(int(last_event_id)):
                        yield sse(event)
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"  # also tells proxies the connection is in use
                        continue
                    yield sse(event)
            finally:
                bus.unsubscribe(loop, queue)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/events")
    def recent_events(since: int = 0, limit: int = Query(50, ge=1, le=200)):
        """Recent events, oldest first: what a page shows in its feed before the stream takes over."""
        return bus.since(since)[-limit:]

    @app.get("/api/live/status")
    def live_status(session: Session = Depends(get_session)):
        from .scheduler import ensemble_lock, next_runs, refresh_lock

        return {
            "now": datetime.now(),
            "listeners": bus.listeners,
            "demo": config.DEMO,
            "scheduler": app.state.scheduler is not None,
            "next_runs": next_runs(app.state.scheduler),
            "refresh_running": refresh_lock.locked(),
            "ensemble_running": ensemble_lock.locked(),
            "freshness": freshness(session, rules),
            "ensemble_run_at": session.scalar(select(func.max(EnsembleDaily.run_at))),
            "sim": station.state.public(),
            "scenarios": SCENARIOS,
        }

    @app.post("/api/refresh", status_code=202, dependencies=[Depends(demo)])
    def refresh_now():
        """Run the forecast refresh now, in the background; progress arrives on /api/stream."""
        from .scheduler import refresh, refresh_lock

        if refresh_lock.locked():
            raise HTTPException(409, "a refresh is already running")
        threading.Thread(target=refresh, args=(session_factory, "manual"), daemon=True).start()
        return {"started": True}

    @app.post("/api/ensemble/refresh", status_code=202, dependencies=[Depends(demo)])
    def ensemble_now():
        from .scheduler import ensemble_lock, refresh_ensemble

        if ensemble_lock.locked():
            raise HTTPException(409, "an ensemble refresh is already running")
        threading.Thread(target=refresh_ensemble, args=(session_factory,), daemon=True).start()
        return {"started": True}

    @app.get("/api/sim")
    def sim_state():
        return {"demo": config.DEMO, "scenarios": SCENARIOS, **station.state.public()}

    @app.post("/api/sim/start", status_code=202, dependencies=[Depends(demo)])
    def sim_start(body: SimStart):
        try:
            return station.start(body.district_id, body.scenario, body.seconds_per_hour)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/sim/stop", dependencies=[Depends(demo)])
    def sim_stop():
        return station.stop()

    @app.post("/api/sim/reset", dependencies=[Depends(demo)])
    def sim_reset():
        try:
            return station.reset()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    # ---- Ensemble ------------------------------------------------------------------------

    @app.get("/api/ensemble")
    def ensemble_outlook(session: Session = Depends(get_session)):
        """P(High) per district and night from the ensemble members, per model and pooled, next to the
        deterministic level. `ensemble_action` is an experimental rule, shown for comparison only."""
        today = date.today()
        det = {(r.district_id, r.date): r for r in session.scalars(
            select(RiskDaily).where(RiskDaily.model_version == model_version, RiskDaily.date >= today))}
        rows = session.scalars(
            select(EnsembleDaily).where(EnsembleDaily.model_version == model_version, EnsembleDaily.date >= today,
                                        EnsembleDaily.date < today + timedelta(days=7))
            .order_by(EnsembleDaily.date)
        ).all()
        by_district: dict[int, dict[date, dict]] = {}
        for e in rows:
            entry = by_district.setdefault(e.district_id, {}).setdefault(e.date, {"date": e.date, "models": {}})
            values = {"members": e.members, "p_high": e.p_high, "p_moderate": e.p_moderate,
                      "score_p10": e.score_p10, "score_p50": e.score_p50, "score_p90": e.score_p90,
                      "score_mean": e.score_mean, "wet_hours_p50": e.wet_hours_p50}
            if e.model == ensemble.POOLED:
                entry.update(values)
            else:
                entry["models"][e.model] = values
        result = []
        for d in session.scalars(select(District).order_by(District.name)):
            days = []
            for day, entry in sorted(by_district.get(d.id, {}).items()):
                if "p_high" not in entry:
                    continue
                r = det.get((d.id, day))
                days.append({**entry, "level": r.level if r else None, "score": r.score if r else None})
            result.append({"id": d.id, "name": d.name, "name_ta": d.name_ta, "lat": d.lat, "lon": d.lon,
                           "days": days, "ensemble_action": ensemble.ensemble_action(days, rules, today)})
        return {
            "date": today, "model_version": model_version,
            "run_at": max((e.run_at for e in rows), default=None),
            "models": {k: {"label": label, "members": n} for k, (_, label, n) in ensemble.MODELS.items()},
            "levels": rules["levels"], "alert_rule": alert_rules,
            "alert_p": rules.get("ensemble", {}).get("alert_p"),
            "districts": result,
        }

    # ---- Blocks ---------------------------------------------------------------------------

    @app.get("/api/blocks")
    def blocks_on(day: date | None = Query(None, alias="date"), session: Session = Depends(get_session)):
        """Every block's risk on one date (default today), for the map."""
        day = day or date.today()
        risk = {r.block_id: r for r in session.scalars(
            select(BlockRiskDaily).where(BlockRiskDaily.model_version == model_version, BlockRiskDaily.date == day))}
        return {
            "date": day, "fresh": blocks.fresh(session, rules),
            "blocks": [{"id": b.id, "district_id": b.district_id, "name": b.name, "name_ta": b.name_ta,
                        "lat": b.lat, "lon": b.lon,
                        "level": risk[b.id].level if b.id in risk else None,
                        "score": risk[b.id].score if b.id in risk else None,
                        "longest_wet_run": risk[b.id].longest_wet_run if b.id in risk else None}
                       for b in session.scalars(select(Block).order_by(Block.district_id, Block.name))],
        }

    @app.get("/api/districts/{district_id}/blocks")
    def district_blocks(district_id: int, session: Session = Depends(get_session)):
        """Each block of a district over the next 7 days, next to the district headquarters' level."""
        if session.get(District, district_id) is None:
            raise HTTPException(404, "district not found")
        today = date.today()
        hq = {r.date: r.level for r in session.scalars(
            select(RiskDaily).where(RiskDaily.district_id == district_id, RiskDaily.model_version == model_version,
                                    RiskDaily.date >= today, RiskDaily.date < today + timedelta(days=7)))}
        series: dict[int, list] = {}
        for r in session.scalars(
            select(BlockRiskDaily).join(Block, Block.id == BlockRiskDaily.block_id)
            .where(Block.district_id == district_id, BlockRiskDaily.model_version == model_version,
                   BlockRiskDaily.date >= today, BlockRiskDaily.date < today + timedelta(days=7))
            .order_by(BlockRiskDaily.date)
        ):
            series.setdefault(r.block_id, []).append(
                {"date": r.date, "level": r.level, "score": r.score, "longest_wet_run": r.longest_wet_run,
                 "wet_hours": r.wet_hours})
        horizon = alert_rules["horizon_days"]
        district_blocks_ = session.scalars(select(Block).where(Block.district_id == district_id)
                                           .order_by(Block.name)).all()
        farmers_in: dict[int, int] = {}
        for sub in session.scalars(select(Subscriber).where(Subscriber.active, Subscriber.district_id == district_id)):
            b = blocks.resolve(session, district_id, sub.block)
            if b is not None:
                farmers_in[b.id] = farmers_in.get(b.id, 0) + 1
        rank = {"Low": 0, "Moderate": 1, "High": 2}
        rows = []
        for b in district_blocks_:
            days = series.get(b.id, [])
            near = days[:horizon]
            worst = max(near, key=lambda x: (rank[x["level"]], x["score"]), default=None)
            rows.append({
                "id": b.id, "name": b.name, "name_ta": b.name_ta, "lat": b.lat, "lon": b.lon,
                "hq_place": b.hq_place, "located_by": b.located_by, "days": days,
                "max_score": worst["score"] if worst else None, "max_level": worst["level"] if worst else None,
                "max_date": worst["date"] if worst else None,
                "longest_wet_run": max((x["longest_wet_run"] for x in near), default=None),
                "high_days": sum(x["level"] == "High" for x in near),
                "alert": sum(x["level"] == "High" for x in near) >= alert_rules["min_high_days"],
                "farmers": farmers_in.get(b.id, 0),
            })
        rows.sort(key=lambda b: (-(b["max_score"] if b["max_score"] is not None else -1), b["name"]))
        return {"district_id": district_id, "fresh": blocks.fresh(session, rules),
                "hq": [{"date": k, "level": v} for k, v in sorted(hq.items())], "blocks": rows}

    @app.get("/api/rules/compare")
    def rules_compare(days: int = Query(30, ge=1, le=366), ahead: int = Query(6, ge=0, le=6),
                      session: Session = Depends(get_session)):
        """Rule A (district headquarters) vs Rule B (half the blocks) over the last `days` days and the
        next `ahead` forecast days."""
        today = date.today()
        return blocks.compare_rules(session, rules, today - timedelta(days=days), today + timedelta(days=ahead))

    @app.get("/api/validation")
    def validation(session: Session = Depends(get_session)):
        """How far the model is from being validated: labelled observations against the pre-registered
        minimums in config/eval_criteria.toml, and the audit of public surveillance reports."""
        import csv
        import tomllib

        criteria = tomllib.loads(config.EVAL_CRITERIA_PATH.read_text(encoding="utf-8"))["pass"]
        obs = session.scalars(select(Observation)).all()
        audit_path = config.ROOT / "docs" / "tnau_surveillance_audit.csv"
        audit = list(csv.DictReader(open(audit_path, encoding="utf-8"))) if audit_path.exists() else []
        kinds = {}
        for r in audit:
            kind = r["usable_for_pilot"].split(" (")[0]
            kinds[kind] = kinds.get(kind, 0) + 1
        return {
            "observations": {
                "present": sum(o.blast_present for o in obs), "absent": sum(not o.blast_present for o in obs),
                "districts_with_present": len({o.district_id for o in obs if o.blast_present}),
                "from_field_checks": session.scalar(select(func.count(FieldCheck.id))),
                "from_farmer_reports": session.scalar(select(func.count(Followup.id)).where(
                    Followup.officer_status == "confirmed")),
            },
            "required": {"present": criteria["min_present"], "absent": criteria["min_absent"],
                         "districts_with_present": criteria["min_districts_with_present"],
                         "min_precision": criteria["min_precision"], "min_recall": criteria["min_recall"]},
            "audit": {
                "source": "TNAU Centre for Plant Protection Studies, monthly Pest and Disease Surveillance and "
                          "Forecast reports (agritech.tnau.ac.in/crop_protection/surv_fc_reports_en.html)",
                "reports": len(audit), "first": audit[0]["month"] if audit else None,
                "last": audit[-1]["month"] if audit else None, "by_kind": kinds,
                "records": [r for r in audit if r["rice_blast_statement"]],
            },
        }

    @app.get("/api/blocks/{block_id}/message")
    def block_message(block_id: int, session: Session = Depends(get_session)):
        """The farmer message for one block's WhatsApp group, when that block meets the alert rule on
        fresh data; otherwise `message` is null and `reason` says why."""
        block = session.get(Block, block_id)
        if block is None:
            raise HTTPException(404, "block not found")
        district = session.get(District, block.district_id)
        today = date.today()
        high = blocks.upcoming_high_days(session, block.id, rules, today)
        base = {"block_id": block.id, "block": block.name, "district": district.name,
                "high_days": high, "message": None}
        if freshness(session, rules)["status"] == "expired" or not blocks.fresh(session, rules):
            return {**base, "reason": "forecast out of date"}
        if len(high) < alert_rules["min_high_days"]:
            return {**base, "reason": f"fewer than {alert_rules['min_high_days']} High days in the next "
                                      f"{alert_rules['horizon_days']}"}
        en, ta = alerts_mod.compose(district, high, block)
        return {**base, "reason": None, "message": {"en": en, "ta": ta}, "generated_at": datetime.now()}

    @app.get("/api/districts")
    def districts(session: Session = Depends(get_session)):
        return [
            {"id": d.id, "name": d.name, "name_ta": d.name_ta, "state": d.state, "lat": d.lat,
             "lon": d.lon}
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

        rows = session.execute(
            select(District, RiskDaily)
            .join(RiskDaily, (RiskDaily.district_id == District.id)
                  & (RiskDaily.date == day) & (RiskDaily.model_version == model_version))
            .order_by(RiskDaily.score.desc())
        ).all()
        result = []
        for d, r in rows:
            result.append({
                "id": d.id, "name": d.name, "state": d.state, "lat": d.lat, "lon": d.lon,
                "score": r.score, "level": r.level, "wet_hours": r.wet_hours,
                "longest_wet_run": r.longest_wet_run, "rain_mm": r.rain_mm,
                "mean_temp_c": r.mean_temp_c, "is_forecast": r.is_forecast,
                "wetness_basis": r.wetness_basis,
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

    if (config.WEB_DIR / "index.html").exists():
        # Hashed JS/CSS bundles; every other path is a page of the single-page app, so unknown paths
        # (e.g. /live, /confidence) return index.html and the browser-side router takes over.
        app.mount("/assets", StaticFiles(directory=config.WEB_DIR / "assets"), name="assets")
        index = config.WEB_DIR / "index.html"

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404, "not found")
            file = (config.WEB_DIR / path).resolve()
            if path and file.is_file() and file.is_relative_to(config.WEB_DIR.resolve()):
                return FileResponse(file)
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return app
