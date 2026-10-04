"""Glue between the database and the pure risk engine, plus seed loading."""
import csv
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import upsert
from .models import District, Gene, RiskDaily, Variety, WeatherHourly
from .risk import HourObs, load_rules, score_series

# When two sources cover the same hour, prefer the live one.
SOURCE_PRIORITY = {"open-meteo": 0, "nasa-power": 1}


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: (v if v != "" else None) for k, v in row.items()} for row in csv.DictReader(f)]


def load_seeds(session: Session, seed_dir: Path = config.SEED_DIR) -> dict[str, int]:
    districts = _read_csv(seed_dir / "districts.csv")
    for d in districts:
        d["lat"], d["lon"] = float(d["lat"]), float(d["lon"])
        d["rice_area_ha"] = float(d["rice_area_ha"]) if d["rice_area_ha"] else None
    genes = _read_csv(seed_dir / "genes.csv")
    varieties = _read_csv(seed_dir / "varieties.csv")

    counts = {
        "districts": upsert(session, District, districts, ["name", "state"]),
        "genes": upsert(session, Gene, genes, ["symbol"]),
        "varieties": upsert(session, Variety, varieties, ["name"]),
    }
    session.commit()
    return counts


def merged_hours(session: Session, district_id: int) -> list[HourObs]:
    rows = session.scalars(
        select(WeatherHourly).where(WeatherHourly.district_id == district_id).order_by(WeatherHourly.ts)
    ).all()
    best: dict[datetime, WeatherHourly] = {}
    for r in rows:
        current = best.get(r.ts)
        if current is None or SOURCE_PRIORITY.get(r.source, 9) < SOURCE_PRIORITY.get(current.source, 9):
            best[r.ts] = r
    return [
        HourObs(r.ts, r.temp_c, r.rh_pct, r.precip_mm, r.cloud_pct, r.is_forecast)
        for r in sorted(best.values(), key=lambda r: r.ts)
    ]


def compute_risk(session: Session, rules: dict | None = None) -> int:
    rules = rules or load_rules()
    now = datetime.now()
    count = 0
    for district in session.scalars(select(District).order_by(District.id)):
        days = score_series(merged_hours(session, district.id), rules, district.state)
        rows = [
            {
                "district_id": district.id,
                "date": d.date,
                "score": d.score,
                "level": d.level,
                "wet_hours": d.wet_hours,
                "longest_wet_run": d.longest_wet_run,
                "rain_mm": d.rain_mm,
                "mean_temp_c": d.mean_temp_c,
                "susceptibility": d.susceptibility,
                "is_forecast": d.is_forecast,
                "model_version": rules["model_version"],
                "computed_at": now,
            }
            for d in days
        ]
        count += upsert(session, RiskDaily, rows, ["district_id", "date", "model_version"])
    session.commit()
    return count


def split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(";") if v.strip()]


def advice_for(session: Session, district: District, level: str | None) -> dict:
    """Variety and gene suggestions for a district, worded by current risk level."""
    varieties = [v for v in session.scalars(select(Variety)) if district.state in split(v.states)]
    genes = session.scalars(select(Gene).where(Gene.role == "host-resistance")).all()
    messages = {
        "High": "Conditions strongly favour blast infection in the coming days. Scout fields, "
        "avoid extra nitrogen top-dressing, and follow local agricultural-department advice "
        "on protective fungicide use.",
        "Moderate": "Conditions are becoming favourable for blast. Inspect leaves for "
        "spindle-shaped lesions and keep nitrogen balanced.",
        "Low": "Weather is not currently favourable for blast infection.",
    }
    return {
        "level": level,
        "message": messages.get(level, "No risk score available yet."),
        "next_season": "For future sowings, prefer varieties carrying broad-spectrum resistance "
        "genes such as Pi9 or Pi54; stacking two genes gives more durable resistance.",
        "varieties": [
            {
                "name": v.name,
                "genes": split(v.genes),
                "status": v.status,
                "note": v.note,
                "source_url": v.source_url,
            }
            for v in varieties
        ],
        "genes": [g.symbol for g in genes],
    }
