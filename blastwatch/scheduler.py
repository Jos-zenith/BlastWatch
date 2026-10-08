"""Background jobs.

- bootstrap (once at start): load FAOSTAT / GenBank if the database has none yet, so a fresh
  deployment fills itself without a manual `python -m blastwatch all`.
- refresh (every 3 h, and immediately at start): forecast -> risk -> district alerts ->
  farmer alerts and check-ins (farmer messages only go out during send hours). Running at start
  matters on hosts that put idle instances to sleep: the first wake-up refreshes stale data.
- ensemble (every [ensemble] refresh_hours, first run a minute after start): score every member of
  three global ensembles (ensemble.py).

Each step is published on the event bus, so open dashboards follow a refresh as it happens.
A lock keeps a manual "refresh now" from overlapping a scheduled run.
"""
import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import alerts, blocks, ensemble, farmers
from .db import SessionLocal
from .events import publish
from .ingest import faostat, genbank, weather
from .models import Gene, Production
from .pipeline import compute_risk, level_changes, level_snapshot
from .risk import load_rules
from .runs import freshness

log = logging.getLogger("blastwatch.scheduler")
REFRESH_HOURS = 3
refresh_lock = threading.Lock()
ensemble_lock = threading.Lock()


def refresh(session_factory: Callable[[], Session] = SessionLocal, trigger: str = "schedule") -> dict | None:
    """Returns None when a refresh is already running."""
    if not refresh_lock.acquire(blocking=False):
        return None
    try:
        return _refresh(session_factory, trigger)
    finally:
        refresh_lock.release()


def _refresh(session_factory, trigger: str) -> dict:
    rules = load_rules()
    publish("refresh.started", trigger=trigger)
    with session_factory() as session:
        try:
            hours = weather.ingest_forecast(session)
            publish("refresh.weather", rows=hours, freshness=freshness(session, rules))
        except Exception as exc:
            log.exception("all forecast sources failed; keeping previous data")
            publish("refresh.weather_failed", error=f"{type(exc).__name__}: {exc}"[:300])
            hours = 0
        since = date.today() - timedelta(days=1)
        before = level_snapshot(session, rules, since)
        days = compute_risk(session, rules)
        changes = level_changes(session, before, level_snapshot(session, rules, since))
        publish("risk.changed" if changes else "risk.unchanged", cause="forecast refresh", days=days,
                changes=changes)
        try:
            block_hours = blocks.ingest_forecast(session)
            block_days = blocks.compute_risk(session, rules)
            publish("blocks.updated", hours=block_hours, days=block_days)
        except Exception as exc:
            # District risk and alerts do not depend on blocks; farmers fall back to district risk.
            log.exception("block forecast failed")
            publish("blocks.failed", error=f"{type(exc).__name__}: {exc}"[:300])
        result = alerts.evaluate(session, rules)
        publish("alerts.evaluated", cause="forecast refresh", **result)
        farmer_result = farmers.notify_farmers(session, rules)
        log.info("refresh: %s weather hours, %s risk days, alerts %s, farmers %s",
                 hours, days, result, farmer_result)
        summary = {"weather_hours": hours, "risk_days": days, "alerts": result, "farmers": farmer_result}
        publish("refresh.done", trigger=trigger, weather_hours=hours, risk_days=days, changed=len(changes))
        return summary


def refresh_ensemble(session_factory: Callable[[], Session] = SessionLocal) -> dict | None:
    if not ensemble_lock.acquire(blocking=False):
        return None
    try:
        publish("ensemble.started")
        with session_factory() as session:
            return ensemble.ingest(session, load_rules())
    except Exception:
        log.exception("ensemble refresh failed")
        return None
    finally:
        ensemble_lock.release()


def bootstrap(session_factory: Callable[[], Session] = SessionLocal) -> None:
    with session_factory() as session:
        if not session.scalar(select(func.count(Production.id))):
            try:
                log.info("bootstrap: FAOSTAT rows %s", faostat.ingest(session))
            except Exception:
                log.exception("bootstrap: FAOSTAT load failed")
        if not session.scalar(select(func.count(Gene.id)).where(Gene.fetched_at.is_not(None))):
            try:
                log.info("bootstrap: GenBank records %s", genbank.ingest(session))
            except Exception:
                log.exception("bootstrap: GenBank load failed")


def _add_jobs(sched, session_factory) -> None:
    now = datetime.now()
    ensemble_hours = load_rules().get("ensemble", {}).get("refresh_hours", 6)
    sched.add_job(bootstrap, args=[session_factory], next_run_time=now, id="bootstrap")
    sched.add_job(refresh, "interval", hours=REFRESH_HOURS, args=[session_factory],
                  next_run_time=now, id="refresh", max_instances=1, coalesce=True)
    sched.add_job(refresh_ensemble, "interval", hours=ensemble_hours, args=[session_factory],
                  next_run_time=now + timedelta(minutes=1), id="ensemble", max_instances=1, coalesce=True)


def next_runs(sched) -> dict:
    if sched is None:
        return {}
    return {job.id: job.next_run_time for job in sched.get_jobs() if job.id in ("refresh", "ensemble")}


def start_background(session_factory: Callable[[], Session] = SessionLocal) -> BackgroundScheduler:
    sched = BackgroundScheduler()
    _add_jobs(sched, session_factory)
    sched.start()
    return sched


def run_forever(session_factory: Callable[[], Session] = SessionLocal) -> None:
    sched = BlockingScheduler()
    _add_jobs(sched, session_factory)
    sched.start()
