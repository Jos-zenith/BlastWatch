"""Background jobs.

- bootstrap (once at start): load FAOSTAT / GenBank if the database has none yet, so a fresh
  deployment fills itself without a manual `python -m blastwatch all`.
- refresh (every 3 h, and immediately at start): forecast -> risk -> alerts. Running at start
  matters on hosts that put idle instances to sleep: the first wake-up refreshes stale data.
"""
import logging
from collections.abc import Callable
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import alerts
from .db import SessionLocal
from .ingest import faostat, genbank, weather
from .models import Gene, Production
from .pipeline import compute_risk
from .risk import load_rules

log = logging.getLogger("blastwatch.scheduler")
REFRESH_HOURS = 3


def refresh(session_factory: Callable[[], Session] = SessionLocal) -> dict:
    rules = load_rules()
    with session_factory() as session:
        try:
            hours = weather.ingest_forecast(session)
        except Exception:
            log.exception("all forecast sources failed; keeping previous data")
            hours = 0
        days = compute_risk(session, rules)
        result = alerts.evaluate(session, rules)
        log.info("refresh: %s weather hours, %s risk days, alerts %s", hours, days, result)
        return {"weather_hours": hours, "risk_days": days, "alerts": result}


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
    sched.add_job(bootstrap, args=[session_factory], next_run_time=now, id="bootstrap")
    sched.add_job(refresh, "interval", hours=REFRESH_HOURS, args=[session_factory],
                  next_run_time=now, id="refresh", max_instances=1, coalesce=True)


def start_background(session_factory: Callable[[], Session] = SessionLocal) -> BackgroundScheduler:
    sched = BackgroundScheduler()
    _add_jobs(sched, session_factory)
    sched.start()
    return sched


def run_forever(session_factory: Callable[[], Session] = SessionLocal) -> None:
    sched = BlockingScheduler()
    _add_jobs(sched, session_factory)
    sched.start()
