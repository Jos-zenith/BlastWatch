"""Crop stage of a subscriber's field, from variety duration and sowing/transplanting date.

Stages are counted back from maturity ([crop_stage] in risk_rules.toml). A variety missing from
seed/crop_calendar.csv has no known duration, so its stage is "unknown": the farmer still gets
alerts, worded to cover every stage, rather than being dropped on a guessed duration.
"""
import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import config

SUSCEPTIBLE = {"vegetative", "booting", "heading", "unknown"}


@dataclass(frozen=True)
class CropVariety:
    name: str
    duration_days: int
    nursery_age_days: int | None
    source: str


@dataclass(frozen=True)
class Stage:
    name: str  # vegetative | booting | heading | ripening | harvested | unknown
    age_days: int
    flags: tuple[str, ...] = ()

    @property
    def susceptible(self) -> bool:
        return self.name in SUSCEPTIBLE


def load_crop_calendar(path: Path = config.SEED_DIR / "crop_calendar.csv") -> dict[str, CropVariety]:
    with open(path, newline="", encoding="utf-8") as f:
        return {
            r["variety"]: CropVariety(r["variety"], int(r["duration_days"]),
                                      int(r["nursery_age_days"]) if r["nursery_age_days"] else None, r["source"])
            for r in csv.DictReader(f)
        }


def crop_stage(variety: str, method: str, establish_date: date, today: date, rules: dict,
               calendar: dict[str, CropVariety]) -> Stage:
    c = rules["crop_stage"]
    v = calendar.get(variety)
    flags = []
    nursery = 0
    if method == "transplanted":
        nursery = v.nursery_age_days if v and v.nursery_age_days is not None else None
        if nursery is None:
            nursery = c["default_nursery_age_days"]
            flags.append("nursery_age_default")
    age = (today - establish_date).days + nursery

    if v is None:
        flags.append("variety_unknown")
        longest = max((x.duration_days for x in calendar.values()), default=150)
        name = "harvested" if age > longest + c["harvested_after_days"] else "unknown"
        return Stage(name, age, tuple(flags))

    to_maturity = v.duration_days - age
    if to_maturity < -c["harvested_after_days"]:
        name = "harvested"
    elif to_maturity <= c["ripening_days"]:
        name = "ripening"
    elif to_maturity <= c["ripening_days"] + c["heading_days"]:
        name = "heading"
    elif to_maturity <= c["ripening_days"] + c["reproductive_days"]:
        name = "booting"
    else:
        name = "vegetative"
    return Stage(name, age, tuple(flags))
