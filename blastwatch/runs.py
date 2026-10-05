"""Ingest-run bookkeeping and data-freshness checks."""
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import IngestRun

FORECAST_SOURCES = ("open-meteo", "met-no")


class RunResult:
    rows: int = 0


@contextmanager
def track(session: Session, source: str):
    """Record an ingest attempt; failures are logged with their error and re-raised."""
    started = datetime.now()
    result = RunResult()
    try:
        yield result
    except Exception as exc:
        session.rollback()
        session.add(IngestRun(source=source, started_at=started, finished_at=datetime.now(),
                              status="failed", error=f"{type(exc).__name__}: {exc}"[:2000]))
        session.commit()
        raise
    session.add(IngestRun(source=source, started_at=started, finished_at=datetime.now(),
                          status="ok", rows=result.rows))
    session.commit()


def last_success(session: Session, sources: tuple[str, ...]) -> datetime | None:
    return session.scalar(
        select(func.max(IngestRun.finished_at)).where(IngestRun.source.in_(sources), IngestRun.status == "ok")
    )


def freshness(session: Session, rules: dict, now: datetime | None = None) -> dict:
    """Forecast freshness: "fresh", "stale" (warn) or "expired" (alerts suppressed)."""
    now = now or datetime.now()
    last = last_success(session, FORECAST_SOURCES)
    policy = rules["freshness"]
    if last is None:
        status, age_h = "expired", None
    else:
        age_h = round((now - last).total_seconds() / 3600, 1)
        status = ("fresh" if age_h < policy["stale_after_hours"]
                  else "stale" if age_h < policy["expire_after_hours"] else "expired")
    last_failure = session.scalars(
        select(IngestRun).where(IngestRun.source.in_(FORECAST_SOURCES), IngestRun.status == "failed")
        .order_by(IngestRun.finished_at.desc()).limit(1)
    ).first()
    source = session.scalar(
        select(IngestRun.source).where(IngestRun.source.in_(FORECAST_SOURCES), IngestRun.status == "ok")
        .order_by(IngestRun.finished_at.desc()).limit(1)
    )
    return {
        "status": status,
        "last_success": last,
        "age_hours": age_h,
        "source": source,
        "last_failure": None if last_failure is None else {
            "source": last_failure.source, "at": last_failure.finished_at, "error": last_failure.error,
        },
    }
