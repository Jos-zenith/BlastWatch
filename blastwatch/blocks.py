"""Block-level risk: the same rules, scored at every development block's headquarters.

A district is scored at one point, its headquarters, but humid pockets and rain bands are smaller
than a district. Blocks (about 10 per district, 15-20 km across) are close to the scale the
forecast grid itself resolves (~9-11 km), so scoring each one shows where in a district the risk
sits, which is what an officer needs in order to decide where to scout.

What changes and what does not:
- The district alert rule is unchanged: it still uses the district headquarters point. Raising an
  alert when ANY of ~10 blocks is High would multiply the alert rate (each block gets its own
  chance of a High night), undoing the base-rate tuning in risk_rules.toml. The officer view shows
  how many blocks are High next to each district day instead.
- A farmer whose enrolment names a known block gets alerts from THAT block's risk. That is one
  point per farmer, the same base rate as before, only closer to the field.
- Sensors still post per district; microclimate inside the canopy is below any grid and only a
  sensor can see it.

Cost: one Open-Meteo call per block per refresh (~190 per 3-hourly refresh, ~1,500 a day).
Blocks use Open-Meteo only; when that fails, block risk ages out (see `fresh`) and farmers fall
back to district risk.
"""
import csv
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import upsert
from .ingest.weather import fetch_open_meteo
from .models import Block, BlockRiskDaily, BlockWeatherHourly, District
from .risk import HourObs, load_rules, score_series
from .runs import last_success, track

SOURCE = "open-meteo-blocks"
KEYS = ["block_id", "ts", "source"]


def load_block_seed(session: Session, path=config.SEED_DIR / "blocks.csv") -> int:
    if not path.exists():
        return 0
    districts = {d.name: d.id for d in session.scalars(select(District))}
    with open(path, newline="", encoding="utf-8") as f:
        rows = [{"district_id": districts[r["district"]], "name": r["name"], "name_ta": r["name_ta"] or None,
                 "lat": float(r["lat"]), "lon": float(r["lon"]), "located_by": r["located_by"],
                 "hq_place": r["hq_place"] or None, "wikidata": r["wikidata"] or None}
                for r in csv.DictReader(f) if r["district"] in districts]
    n = upsert(session, Block, rows, ["district_id", "name"])
    session.commit()
    return n


def ingest_forecast(session: Session, past_days: int = 3, forecast_days: int = 7,
                    client: httpx.Client | None = None) -> int:
    blocks = session.scalars(select(Block).order_by(Block.id)).all()
    if not blocks:
        return 0
    with track(session, SOURCE) as run:
        rows = fetch_open_meteo(blocks, past_days, forecast_days, client)
        # fetch_open_meteo keys rows by the location's id under "district_id"; here that id is a block.
        rows = [{**{k: v for k, v in r.items() if k != "district_id"}, "block_id": r["district_id"], "source": SOURCE}
                for r in rows]
        run.rows = upsert(session, BlockWeatherHourly, rows, KEYS)
    return run.rows


def block_hours(session: Session, block_id: int) -> list[HourObs]:
    rows = session.scalars(select(BlockWeatherHourly).where(BlockWeatherHourly.block_id == block_id)
                           .order_by(BlockWeatherHourly.ts))
    return [HourObs(r.ts, r.temp_c, r.rh_pct, r.precip_mm, r.cloud_pct, r.is_forecast, r.dew_point_c,
                    r.leaf_wet_prob, r.leaf_wet_min) for r in rows]


def compute_risk(session: Session, rules: dict | None = None, since: date | None = None) -> int:
    """Score every block; only days from `since` on are stored (default: yesterday)."""
    rules = rules or load_rules()
    since = since or date.today() - timedelta(days=1)
    states = {d.id: d.state for d in session.scalars(select(District))}
    now, count = datetime.now(), 0
    for block in session.scalars(select(Block).order_by(Block.id)).all():
        hours = [h for h in block_hours(session, block.id) if h.ts >= datetime.combine(since, datetime.min.time())
                 - timedelta(days=1)]
        rows = [{"block_id": block.id, "date": d.date, "score": d.score, "level": d.level, "wet_hours": d.wet_hours,
                 "longest_wet_run": d.longest_wet_run, "rain_mm": d.rain_mm, "wetness_basis": d.wetness_basis,
                 "is_forecast": d.is_forecast, "model_version": rules["model_version"], "computed_at": now}
                for d in score_series(hours, rules, states[block.district_id]) if d.date >= since]
        count += upsert(session, BlockRiskDaily, rows, ["block_id", "date", "model_version"])
    session.commit()
    return count


def fresh(session: Session, rules: dict, now: datetime | None = None) -> bool:
    """Block risk is usable for farmer alerts while its forecast is younger than the expiry age."""
    last = last_success(session, (SOURCE,))
    return last is not None and (now or datetime.now()) - last < timedelta(hours=rules["freshness"]["expire_after_hours"])


def rollup(session: Session, rules: dict, since: date) -> dict[tuple[int, date], dict]:
    """{(district_id, date): {"total", "High", "Moderate", "Low", "max_score", "high_blocks"}}."""
    out: dict[tuple[int, date], dict] = defaultdict(lambda: {"total": 0, "High": 0, "Moderate": 0, "Low": 0,
                                                              "max_score": 0.0, "high_blocks": []})
    rows = session.execute(
        select(BlockRiskDaily, Block).join(Block, Block.id == BlockRiskDaily.block_id)
        .where(BlockRiskDaily.model_version == rules["model_version"], BlockRiskDaily.date >= since)
        .order_by(Block.name))
    for r, b in rows:
        entry = out[(b.district_id, r.date)]
        entry["total"] += 1
        entry[r.level] += 1
        entry["max_score"] = max(entry["max_score"], r.score)
        if r.level == "High":
            entry["high_blocks"].append(b.name)
    return dict(out)


def share_level(counts: dict, share: float) -> str | None:
    """A district's level from its blocks: High when >= `share` of scored blocks are High, Moderate
    when High + Moderate reach that share, else Low."""
    total = counts.get("total", 0)
    if not total:
        return None
    if counts["High"] >= share * total:
        return "High"
    if counts["High"] + counts["Moderate"] >= share * total:
        return "Moderate"
    return "Low"


def share_high_days(session: Session, district_id: int, rules: dict, today: date) -> list[date] | None:
    """Rule B's High days in the alert horizon, or None when it cannot be applied (stale block data,
    or no scored blocks), so the caller falls back to the headquarters."""
    if not fresh(session, rules):
        return None
    a = rules["alerts"]
    roll = {day: c for (d, day), c in rollup(session, rules, today).items()
            if d == district_id and day < today + timedelta(days=a["horizon_days"])}
    if not roll:
        return None
    return sorted(day for day, c in roll.items() if share_level(c, a.get("block_share", 0.5)) == "High")


def upcoming_high_days(session: Session, block_id: int, rules: dict, today: date) -> list[date]:
    horizon = rules["alerts"]["horizon_days"]
    return list(session.scalars(select(BlockRiskDaily.date).where(
        BlockRiskDaily.block_id == block_id, BlockRiskDaily.model_version == rules["model_version"],
        BlockRiskDaily.level == "High", BlockRiskDaily.date >= today,
        BlockRiskDaily.date < today + timedelta(days=horizon)).order_by(BlockRiskDaily.date)))


def _key(name: str) -> str:
    s = re.sub(r"[^a-z]", "", name.lower().removesuffix(" block"))
    s = s.replace("kovil", "koil").replace("pettai", "pet").replace("palaiyam", "palayam")
    for a, b in (("zh", "l"), ("th", "t"), ("dh", "d"), ("w", "v"), ("ee", "i"), ("oo", "u"), ("y", ""), ("ai", "a")):
        s = s.replace(a, b)
    # Voiced/unvoiced pairs are one letter in Tamil script (Thiruvaiyaru / Tiruvayaru, Kudi / Kuti).
    return re.sub(r"(.)\1+", r"\1", s.replace("d", "t").replace("g", "k"))


def resolve(session: Session, district_id: int, name: str | None) -> Block | None:
    """The block an enrolment names, tolerating spelling variants (Thiruvaiyaru / Tiruvaiyaru) and
    Tamil names; None when it matches no block of that district."""
    if not name:
        return None
    blocks = session.scalars(select(Block).where(Block.district_id == district_id)).all()
    key, ta = _key(name), re.sub(r"\s+", "", name)
    for b in blocks:
        if _key(b.name) == key or (b.name_ta and re.sub(r"\s+", "", b.name_ta) == ta):
            return b
    return None



def compare_rules(session: Session, rules: dict, start: date, end: date) -> dict:
    """Rule A (headquarters) vs Rule B (share of blocks High) on every stored district-day in a date
    range. The last score stored for a day is kept, so past days compare on near-analysis weather.
    It fills in as block data accumulates: comparison starts on the first day blocks were scored."""
    from .models import RiskDaily

    share = rules["alerts"].get("block_share", 0.5)
    hq = {(r.district_id, r.date): r.level for r in session.scalars(select(RiskDaily).where(
        RiskDaily.model_version == rules["model_version"], RiskDaily.date >= start, RiskDaily.date <= end))}
    roll = {k: v for k, v in rollup(session, rules, start).items() if k[1] <= end}
    names = {d.id: d.name for d in session.scalars(select(District))}
    per: dict[int, dict] = defaultdict(lambda: {"days": 0, "hq_high": 0, "blocks_high": 0, "both_high": 0, "agree": 0})
    for key, counts in roll.items():
        if key not in hq:
            continue
        a, b = hq[key], share_level(counts, share)
        p = per[key[0]]
        p["days"] += 1
        p["hq_high"] += a == "High"
        p["blocks_high"] += b == "High"
        p["both_high"] += a == "High" and b == "High"
        p["agree"] += a == b
    districts = [{"district": names[d], **v} for d, v in sorted(per.items(), key=lambda kv: names[kv[0]])]
    total = {k: sum(x[k] for x in districts) for k in ("days", "hq_high", "blocks_high", "both_high", "agree")}
    return {"start": start, "end": end, "block_share": share, "total": total, "districts": districts}
