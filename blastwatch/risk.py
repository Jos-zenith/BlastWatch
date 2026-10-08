"""Rule-based blast risk engine (pure functions, no database access)."""
import tomllib
from collections import Counter, defaultdict
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
    dew_point_c: float | None = None
    leaf_wet_prob: float | None = None
    leaf_wet_min: float | None = None


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
    wetness_basis: str | None = None
    mean_cloud_pct: float | None = None


def load_rules(path: Path = config.RULES_PATH) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def susceptibility_for(rules: dict, state: str, month: int) -> float:
    table = rules.get("susceptibility", {})
    entry = table.get(state) or table.get("default") or {}
    return float(entry.get("months", {}).get(str(month), entry.get("default", 1.0)))


def leaf_wetness(obs: HourObs, rules: dict) -> tuple[bool, str | None]:
    """Whether the leaf is wet this hour, using the best available signal, and which one."""
    w = rules["wetness"]
    for method in w["priority"]:
        if method == "sensor" and obs.leaf_wet_min is not None:
            return obs.leaf_wet_min >= w["sensor_min_wet_minutes"], "sensor"
        if method == "lwp" and obs.leaf_wet_prob is not None:
            return obs.leaf_wet_prob >= w["lwp_min_pct"], "lwp"
        if method == "dpd" and obs.dew_point_c is not None and obs.temp_c is not None:
            rained = (obs.precip_mm or 0.0) >= w["rain_wets_leaf_mm"]
            return rained or obs.temp_c - obs.dew_point_c <= w["dpd_max_c"], "dpd"
        if method == "rh" and obs.rh_pct is not None:
            return obs.rh_pct >= w["rh_min_pct"], "rh"
    return False, None


def is_conducive(obs: HourObs, rules: dict) -> bool:
    c = rules["conducive_hour"]
    if obs.temp_c is None or not c["min_temp_c"] <= obs.temp_c <= c["max_temp_c"]:
        return False
    return leaf_wetness(obs, rules)[0]


def window_date(ts: datetime, start_hour: int) -> date:
    """The scored date whose infection-night window contains `ts`."""
    return (ts + timedelta(hours=24 - start_hour)).date() if start_hour else ts.date()


def level_for(score: float, rules: dict) -> str:
    if score >= rules["levels"]["high"]:
        return "High"
    if score >= rules["levels"]["moderate"]:
        return "Moderate"
    return "Low"


def score_points(longest_run: int, wet_hours: int, rain_mm: float, mean_cloud_pct: float | None,
                 rules: dict) -> dict[str, float]:
    """Points per factor before the susceptibility multiplier; the officer view shows them."""
    s = rules["score"]

    def ramp(value: float, start: float, full: float) -> float:
        """0 at or below `start`, 1 at or above `full`, linear between."""
        return min(max((value - start) / (full - start), 0.0), 1.0)

    return {
        "run": round(ramp(longest_run, s.get("run_start_hours", 0), s["full_run_hours"]) * s["run_weight"], 1),
        "hours": round(ramp(wet_hours, s.get("wet_start_hours", 0), s["full_wet_hours"]) * s["hours_weight"], 1),
        "rain": s["rain_weight"] if s["rain_min_mm"] <= rain_mm <= s["rain_max_mm"] else 0,
        "cloud": s["cloud_weight"] if mean_cloud_pct is not None and mean_cloud_pct >= s["cloud_min_pct"] else 0,
    }


def score_window(hours: list[HourObs], rules: dict, susceptibility: float, day: date) -> DayRisk:
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

    bases = Counter(b for b in (leaf_wetness(h, rules)[1] for h in hours) if b)
    rain = sum(h.precip_mm or 0.0 for h in hours)
    temps = [h.temp_c for h in hours if h.temp_c is not None]
    clouds = [h.cloud_pct for h in hours if h.cloud_pct is not None]
    mean_cloud = sum(clouds) / len(clouds) if clouds else None

    raw = sum(score_points(longest, wet_hours, rain, mean_cloud, rules).values())
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
        wetness_basis=bases.most_common(1)[0][0] if bases else None,
        mean_cloud_pct=round(mean_cloud) if mean_cloud is not None else None,
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
