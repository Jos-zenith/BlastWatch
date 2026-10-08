"""Crop stage from establishment date. Transplanted and direct-seeded are separate branches."""
from datetime import date

from .config import load_varieties


def crop_age_days(method: str, establish: date, today: date, nursery_age: int) -> int:
    days = (today - establish).days
    return days + nursery_age if method == "transplanted" else days


def stage_for(sub: dict, today: date, p: dict, varieties: dict | None = None) -> dict:
    v_all = varieties or load_varieties()
    v = v_all.get(sub["variety"], v_all["Other/Unknown"])
    flags = []
    nursery = v.get("nursery_age_days")
    if sub["method"] == "transplanted" and nursery is None:
        nursery = p["establishment"]["default_nursery_age_days"]
        flags.append("nursery_age_default")
    if sub["variety"] not in v_all or v.get("source") == "default":
        flags.append("variety_unknown")
    age = crop_age_days(sub["method"], date.fromisoformat(sub["establish_date"]), today, nursery or 0)
    frac = age / v["duration_days"]
    for s in p["stages"]:
        if s["from"] <= frac < s["to"]:
            return {"stage": s["name"], "susceptible": s["susceptible"], "age_days": age, "flags": flags}
    return {"stage": "outside_season", "susceptible": False, "age_days": age, "flags": flags}
