"""Field-sensor ingest, shared by POST /api/sensors/readings and the virtual station (sim.py).

Readings override grid weather for their district and hour. Only that district is rescored, so a
reading reaches the dashboards within a second, and every level it changes is published live.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import upsert
from .events import publish
from .models import District, RiskDaily, WeatherHourly
from .pipeline import compute_risk, level_changes, level_snapshot
from .risk import window_date


def ingest_readings(session: Session, district: District, station_id: str, readings: list[dict],
                    rules: dict) -> dict:
    """`readings`: dicts with ts (naive local, floored to the hour), temp_c, rh_pct, leaf_wet_min,
    precip_mm. Returns what was stored and the scored day of the latest reading."""
    now = datetime.now()
    rows = {}
    for r in readings:
        rows[r["ts"]] = {"district_id": district.id, "ts": r["ts"], "source": f"sensor:{station_id}",
                         "is_forecast": False, "fetched_at": now, "temp_c": r.get("temp_c"),
                         "rh_pct": r.get("rh_pct"), "precip_mm": r.get("precip_mm"), "cloud_pct": None,
                         "dew_point_c": None, "leaf_wet_prob": None, "leaf_wet_min": r.get("leaf_wet_min")}
    since = date.today() - timedelta(days=1)
    before = level_snapshot(session, rules, since, [district.id])
    upsert(session, WeatherHourly, list(rows.values()), ["district_id", "ts", "source"])
    session.commit()
    compute_risk(session, rules, [district.id])
    changes = level_changes(session, before, level_snapshot(session, rules, since, [district.id]))

    latest = max(rows)
    scored = window_date(latest, rules.get("window_start_hour", 12))
    day = session.scalar(select(RiskDaily).where(
        RiskDaily.district_id == district.id, RiskDaily.date == scored,
        RiskDaily.model_version == rules["model_version"]))
    result = {
        "station": station_id, "district_id": district.id, "district": district.name, "accepted": len(rows),
        "reading": {k: v for k, v in rows[latest].items() if k in ("ts", "temp_c", "rh_pct", "leaf_wet_min", "precip_mm")},
        "day": None if day is None else {
            "date": day.date, "score": day.score, "level": day.level, "wet_hours": day.wet_hours,
            "longest_wet_run": day.longest_wet_run, "wetness_basis": day.wetness_basis},
    }
    publish("sensor.reading", **result)
    if changes:
        publish("risk.changed", cause=f"sensor {station_id}", changes=changes)
    return {**result, "changes": changes}
