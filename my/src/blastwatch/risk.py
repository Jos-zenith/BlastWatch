"""Transparent rule-based risk. No weighted sums with invented coefficients:
count blast-favorable hours per day, alert when enough days in the window qualify.
All thresholds come from config/params.yaml and are hypotheses."""
from collections import defaultdict
from datetime import date, timedelta


def is_favorable(row: dict, p: dict) -> bool:
    f = p["favorable_hour"]
    if row["temp"] is None or row["rh"] is None:
        return False
    if not (f["temp_min"] <= row["temp"] <= f["temp_max"]):
        return False
    wet = row["rh"] >= f["rh_min"]
    if f["rain_counts_as_wet"] and (row["precip"] or 0) >= f["rain_mm"]:
        wet = True
    if not wet:
        return False
    if f["use_wind"] and row["wind"] is not None and row["wind"] >= f["wind_max_ms"]:
        return False
    return True


def daily_favorable_hours(rows: list[dict], p: dict) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        out[r["ts"][:10]] += 1 if is_favorable(r, p) else 0
    return dict(out)


def block_level(day_hours: dict[str, int], start: date, p: dict) -> dict:
    a = p["alert"]
    days = [(start + timedelta(d)).isoformat() for d in range(a["window_days"])]
    present = [d for d in days if d in day_hours]
    fav = [d for d in present if day_hours[d] >= a["min_favorable_hours_per_day"]]
    if len(present) < a["window_days"]:
        level = "NO_DATA"  # never alert on an incomplete forecast
    elif len(fav) >= a["min_favorable_days"]:
        level = "HIGH"
    elif len(fav) >= a["watch_min_favorable_days"]:
        level = "WATCH"
    else:
        level = "LOW"
    return {"level": level, "favorable_days": len(fav), "window_start": days[0], "window_end": days[-1],
            "hours": {d: day_hours.get(d) for d in days}}


def continuous_score(day_hours: dict[str, int], start: date, window: int) -> float:
    """Mean favorable hours over the window; used to rank block-days for PR-AUC."""
    vals = [day_hours.get((start + timedelta(d)).isoformat()) for d in range(window)]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else 0.0
