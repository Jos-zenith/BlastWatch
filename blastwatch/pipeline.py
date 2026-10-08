"""Glue between the database and the pure risk engine, plus seed loading."""
import csv
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import config
from .db import upsert
from .models import District, Gene, RiskDaily, Variety, WeatherHourly
from .risk import HourObs, has_wetness_signal, load_rules, score_series

# When several sources cover the same hour: field sensors, then live forecasts, then history.
SOURCE_PRIORITY = {"sensor": 0, "open-meteo": 1, "met-no": 2, "nasa-power": 3, "archive-forecast": 4}


def source_rank(source: str) -> int:
    return SOURCE_PRIORITY.get(source.split(":", 1)[0], 9)


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: (v if v != "" else None) for k, v in row.items()} for row in csv.DictReader(f)]


def load_seeds(session: Session, seed_dir: Path = config.SEED_DIR) -> dict[str, int]:
    districts = _read_csv(seed_dir / "districts.csv")
    for d in districts:
        d["lat"], d["lon"] = float(d["lat"]), float(d["lon"])
    genes = _read_csv(seed_dir / "genes.csv")
    varieties = _read_csv(seed_dir / "varieties.csv")

    counts = {
        "districts": upsert(session, District, districts, ["name", "state"]),
        "genes": upsert(session, Gene, genes, ["symbol"]),
        "varieties": upsert(session, Variety, varieties, ["name"]),
    }
    session.commit()
    from .blocks import load_block_seed  # blocks reference districts, so they load after them

    counts["blocks"] = load_block_seed(session, seed_dir / "blocks.csv")
    return counts


def merged_hours(session: Session, district_id: int, source: str | None = None) -> list[HourObs]:
    """Best row per hour across sources, or only `source` (exact, or a prefix: "archive-forecast"
    matches "archive-forecast:d3"), so a backtest can pin the weather it is scored on."""
    query = select(WeatherHourly).where(WeatherHourly.district_id == district_id)
    if source:
        query = query.where(or_(WeatherHourly.source == source, WeatherHourly.source.like(f"{source}:%")))
    rows = session.scalars(query.order_by(WeatherHourly.ts)).all()
    best: dict[datetime, tuple[bool, int, HourObs]] = {}
    for r in rows:
        obs = HourObs(r.ts, r.temp_c, r.rh_pct, r.precip_mm, r.cloud_pct, r.is_forecast,
                      r.dew_point_c, r.leaf_wet_prob, r.leaf_wet_min)
        # A row without temperature or any wetness signal (e.g. a sensor reporting only
        # temperature) must not hide a complete row from a lower-priority source.
        key = (not (obs.temp_c is not None and has_wetness_signal(obs)), source_rank(r.source))
        current = best.get(r.ts)
        if current is None or key < current[:2]:
            best[r.ts] = (*key, obs)
    return [best[ts][2] for ts in sorted(best)]


def level_snapshot(session: Session, rules: dict, since: date, district_ids: list[int] | None = None) -> dict:
    """{(district_id, date): (level, score)} from `since` on, to report what a recompute changed."""
    query = select(RiskDaily).where(RiskDaily.model_version == rules["model_version"], RiskDaily.date >= since)
    if district_ids is not None:
        query = query.where(RiskDaily.district_id.in_(district_ids))
    return {(r.district_id, r.date): (r.level, r.score) for r in session.scalars(query)}


def level_changes(session: Session, before: dict, after: dict) -> list[dict]:
    """Days whose level changed between two snapshots (new days count as changes from None)."""
    names = {d.id: d.name for d in session.scalars(select(District))}
    changes = []
    for key in sorted(after, key=lambda k: (k[1], names.get(k[0], ""))):
        old = before.get(key)
        if old is None or old[0] != after[key][0]:
            changes.append({"district_id": key[0], "district": names.get(key[0]), "date": key[1],
                            "from": old[0] if old else None, "to": after[key][0], "score": after[key][1]})
    return changes


def compute_risk(session: Session, rules: dict | None = None, district_ids: list[int] | None = None) -> int:
    """Score every district, or only `district_ids` (a sensor upload touches one district)."""
    rules = rules or load_rules()
    now = datetime.now()
    count = 0
    query = select(District).order_by(District.id)
    if district_ids is not None:
        query = query.where(District.id.in_(district_ids))
    for district in session.scalars(query).all():
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
                "mean_cloud_pct": d.mean_cloud_pct,
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
    # Breeding lines cannot be bought, so only released varieties are offered.
    varieties = [v for v in session.scalars(select(Variety))
                 if district.state in split(v.states) and v.status == "released"]
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
        # Gene presence does not predict field resistance to local races, so no gene-based advice:
        # variety advice waits for TNAU field ratings by season.
        "next_season": None,
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
