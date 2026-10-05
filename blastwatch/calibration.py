"""Calibrate the risk model against field observations.

Workflow:
1. Collect field-confirmed presence/absence records (KVK / agriculture-department pest
   surveillance, TNAU reports, published epidemiology) into a CSV shaped like
   seed/observations_template.csv, then `python -m blastwatch observations import FILE`.
2. Load matching weather: `python -m blastwatch backfill --start ... --end ...`.
3. `python -m blastwatch backtest --start ... --end ...` scores every observation and sweeps
   the High threshold so the trade-off between missed outbreaks and false alarms is explicit.

Absence records matter as much as presence records: without them false alarms cannot be
measured, and the model can look perfect simply by calling everything High.
"""
import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import upsert
from .models import District, Observation
from .pipeline import merged_hours
from .risk import score_series

TRUE_VALUES = {"1", "true", "yes", "y"}


def import_observations(session: Session, path: Path) -> int:
    districts = {(d.name.lower(), d.state.lower()): d.id for d in session.scalars(select(District))}
    rows, unknown = [], set()
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = (r["district"].strip().lower(), r["state"].strip().lower())
            if key not in districts:
                unknown.add(f"{r['district']}, {r['state']}")
                continue
            rows.append({
                "district_id": districts[key],
                "date": date.fromisoformat(r["date"].strip()),
                "blast_present": r["blast_present"].strip().lower() in TRUE_VALUES,
                "severity": (r.get("severity") or "").strip() or None,
                "source": r["source"].strip(),
                "note": (r.get("note") or "").strip() or None,
            })
    if unknown:
        raise ValueError(f"unknown districts (add them to seed/districts.csv first): {sorted(unknown)}")
    count = upsert(session, Observation, rows, ["district_id", "date", "source"])
    session.commit()
    return count


@dataclass
class Scores:
    threshold: float
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def pod(self) -> float | None:  # probability of detection (sensitivity)
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def far(self) -> float | None:  # false alarm ratio: alerts that were wrong
        return self.fp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def specificity(self) -> float | None:
        return self.tn / (self.tn + self.fp) if self.tn + self.fp else None

    @property
    def csi(self) -> float | None:  # critical success index
        return self.tp / (self.tp + self.fp + self.fn) if self.tp + self.fp + self.fn else None


def contingency(
    observations: list[tuple[int, date, bool]],
    daily_scores: dict[tuple[int, date], float],
    threshold: float,
    lead_days: int,
) -> tuple[Scores, int]:
    """An observation counts as "warned" if any day in [date - lead_days, date] scored at or
    above `threshold`. Observations with no scored day in that window are skipped."""
    tp = fp = fn = tn = skipped = 0
    for district_id, day, present in observations:
        window = [daily_scores.get((district_id, day - timedelta(days=k))) for k in range(lead_days + 1)]
        window = [s for s in window if s is not None]
        if not window:
            skipped += 1
            continue
        warned = max(window) >= threshold
        if present and warned:
            tp += 1
        elif present:
            fn += 1
        elif warned:
            fp += 1
        else:
            tn += 1
    return Scores(threshold, tp, fp, fn, tn), skipped


def backtest(session: Session, rules: dict, start: date, end: date, lead_days: int = 3,
             thresholds: list[float] | None = None) -> dict:
    thresholds = thresholds or list(range(30, 95, 5))
    obs = session.scalars(
        select(Observation).where(Observation.date >= start, Observation.date <= end)
    ).all()
    districts = {d.id: d for d in session.scalars(select(District))}
    daily: dict[tuple[int, date], float] = {}
    for district_id in {o.district_id for o in obs}:
        for day in score_series(merged_hours(session, district_id), rules, districts[district_id].state):
            daily[(district_id, day.date)] = day.score
    pairs = [(o.district_id, o.date, o.blast_present) for o in obs]
    results = [contingency(pairs, daily, t, lead_days) for t in thresholds]
    return {
        "observations": len(obs),
        "present": sum(o.blast_present for o in obs),
        "absent": sum(not o.blast_present for o in obs),
        "skipped_no_weather": results[0][1] if results else 0,
        "current_high": rules["levels"]["high"],
        "results": [r for r, _ in results],
    }
