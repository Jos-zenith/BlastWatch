"""Rule-based blast risk engine (pure functions, no database access)."""
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config

LEVELS = ("Low", "Moderate", "High")


@dataclass(frozen=True)
class HourObs:
    ts: datetime
    temp_c: float | None
    rh_pct: float | None
    precip_mm: float | None = None
    cloud_pct: float | None = None
    is_forecast: bool = False


@dataclass(frozen=True)
class DayRisk:
    date: date
    score: float
    level: str
    wet_hours: int
    longest_wet_run: int
    rain_mm: float
    mean_temp_c: float | None
    susceptibility: float
    is_forecast: bool


def load_rules(path: Path = config.RULES_PATH) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def susceptibility_for(rules: dict, state: str, month: int) -> float:
    table = rules.get("susceptibility", {})
    entry = table.get(state) or table.get("default") or {}
    return float(entry.get("months", {}).get(str(month), entry.get("default", 1.0)))


def is_conducive(obs: HourObs, rules: dict) -> bool:
    c = rules["conducive_hour"]
    if obs.temp_c is None or obs.rh_pct is None:
        return False
    return obs.rh_pct >= c["min_rh_pct"] and c["min_temp_c"] <= obs.temp_c <= c["max_temp_c"]


def window_date(ts: datetime, start_hour: int) -> date:
    """The scored date whose infection-night window contains `ts`."""
    return (ts + timedelta(hours=24 - start_hour)).date() if start_hour else ts.date()


def level_for(score: float, rules: dict) -> str:
    if score >= rules["levels"]["high"]:
        return "High"
    if score >= rules["levels"]["moderate"]:
        return "Moderate"
    return "Low"


def score_window(hours: list[HourObs], rules: dict, susceptibility: float, day: date) -> DayRisk:
    s = rules["score"]
    hours = sorted(hours, key=lambda h: h.ts)

    wet_hours = longest = run = 0
    prev_ts = None
    for h in hours:
        contiguous = prev_ts is not None and h.ts - prev_ts == timedelta(hours=1)
        if is_conducive(h, rules):
            wet_hours += 1
            run = run + 1 if contiguous and run else 1
            longest = max(longest, run)
        else:
            run = 0
        prev_ts = h.ts

    rain = sum(h.precip_mm or 0.0 for h in hours)
    temps = [h.temp_c for h in hours if h.temp_c is not None]
    clouds = [h.cloud_pct for h in hours if h.cloud_pct is not None]
    mean_cloud = sum(clouds) / len(clouds) if clouds else None

    raw = min(longest / s["full_run_hours"], 1.0) * s["run_weight"]
    raw += min(wet_hours / s["full_wet_hours"], 1.0) * s["hours_weight"]
    if s["rain_min_mm"] <= rain <= s["rain_max_mm"]:
        raw += s["rain_weight"]
    if mean_cloud is not None and mean_cloud >= s["cloud_min_pct"]:
        raw += s["cloud_weight"]

    score = round(raw * susceptibility, 1)
    return DayRisk(
        date=day,
        score=score,
        level=level_for(score, rules),
        wet_hours=wet_hours,
        longest_wet_run=longest,
        rain_mm=round(rain, 1),
        mean_temp_c=round(sum(temps) / len(temps), 1) if temps else None,
        susceptibility=susceptibility,
        is_forecast=any(h.is_forecast for h in hours),
    )


def score_series(hours: list[HourObs], rules: dict, state: str) -> list[DayRisk]:
    """Score every complete infection-night window in an hourly series."""
    start_hour = rules.get("window_start_hour", 12)
    windows: dict[date, list[HourObs]] = defaultdict(list)
    for h in hours:
        windows[window_date(h.ts, start_hour)].append(h)

    results = []
    for day in sorted(windows):
        bucket = windows[day]
        if len(bucket) < rules.get("min_hours_per_window", 20):
            continue
        results.append(
            score_window(bucket, rules, susceptibility_for(rules, state, day.month), day)
        )
    return results
