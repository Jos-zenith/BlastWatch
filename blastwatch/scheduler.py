"""Periodic refresh: pull the latest forecast, then recompute risk."""
import logging
from collections.abc import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import Session

from .db import SessionLocal
from .ingest import weather
from .pipeline import compute_risk

log = logging.getLogger("blastwatch.scheduler")
REFRESH_HOURS = 3


def refresh(session_factory: Callable[[], Session] = SessionLocal) -> None:
    with session_factory() as session:
        try:
            hours = weather.ingest_forecast(session)
            days = compute_risk(session)
            log.info("refresh ok: %s weather hours, %s risk days", hours, days)
        except Exception:
            log.exception("refresh failed")


def _add_job(sched, session_factory) -> None:
    from datetime import datetime

    sched.add_job(refresh, "interval", hours=REFRESH_HOURS, args=[session_factory],
                  next_run_time=datetime.now(), id="refresh", max_instances=1, coalesce=True)


def start_background(session_factory: Callable[[], Session] = SessionLocal) -> BackgroundScheduler:
    sched = BackgroundScheduler()
    _add_job(sched, session_factory)
    sched.start()
    return sched


def run_forever(session_factory: Callable[[], Session] = SessionLocal) -> None:
    sched = BlockingScheduler()
    _add_job(sched, session_factory)
    sched.start()
