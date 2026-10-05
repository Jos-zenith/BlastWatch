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

# When several sources cover the same hour: field sensors, then live forecasts, then history.
SOURCE_PRIORITY = {"sensor": 0, "open-meteo": 1, "met-no": 2, "nasa-power": 3}


def source_rank(source: str) -> int:
    return SOURCE_PRIORITY.get(source.split(":", 1)[0], 9)


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: (v if v != "" else None) for k, v in row.items()} for row in csv.DictReader(f)]


def load_seeds(session: Session, seed_dir: Path = config.SEED_DIR) -> dict[str, int]:
    districts = _read_csv(seed_dir / "districts.csv")
    for d in districts:
        d["lat"], d["lon"] = float(d["lat"]), float(d["lon"])
        d["rice_area_ha"] = float(d["rice_area_ha"]) if d["rice_area_ha"] else None
    # Seeds run on every startup; a blank rice area in the CSV must not erase a stored value.
    with_area = [d for d in districts if d["rice_area_ha"] is not None]
    without_area = [{k: v for k, v in d.items() if k != "rice_area_ha"} for d in districts
                    if d["rice_area_ha"] is None]
    genes = _read_csv(seed_dir / "genes.csv")
    varieties = _read_csv(seed_dir / "varieties.csv")

    counts = {
        "districts": upsert(session, District, with_area, ["name", "state"])
        + upsert(session, District, without_area, ["name", "state"]),
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
        if current is None or source_rank(r.source) < source_rank(current.source):
            best[r.ts] = r
    return [
        HourObs(r.ts, r.temp_c, r.rh_pct, r.precip_mm, r.cloud_pct, r.is_forecast,
                r.dew_point_c, r.leaf_wet_prob, r.leaf_wet_min)
        for r in sorted(best.values(), key=lambda r: r.ts)
    ]


def compute_risk(session: Session, rules: dict | None = None) -> int:
    rules = rules or load_rules()
    now = datetime.now()
    count = 0
    for district in session.scalars(select(District).order_by(District.id)).all():
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
                "wetness_basis": d.wetness_basis,
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
    # The model is uncalibrated, so the action is always to verify in the field first:
    # spraying on a forecast alone wastes fungicide when the forecast is a false alarm.
    messages = {
        "High": "Weather strongly favours blast in the coming days. Scout fields now and look "
        "for spindle-shaped leaf lesions; in fields at flowering, also check the panicle neck for "
        "dark rot (neck blast). Spray only if lesions are found, as advised by the "
        "local agricultural officer. Avoid extra nitrogen top-dressing.",
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
