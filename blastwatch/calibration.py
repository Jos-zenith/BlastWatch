"""Calibrate the risk model against field observations.

Workflow:
1. Collect field-confirmed presence/absence records (KVK / agriculture-department pest
   surveillance, TNAU reports, published epidemiology) into a CSV shaped like
   seed/observations_template.csv, then `python -m blastwatch observations import FILE`.
2. Load matching weather: `python -m blastwatch backfill --start ... --end ...` (archived forecasts
   by default, so the backtest sees forecast errors as the live system does).
3. `python -m blastwatch backtest --start ... --end ... --weather archive-forecast:d3` scores every observation and sweeps
   the High threshold so the trade-off between missed outbreaks and false alarms is explicit.

`python -m blastwatch evaluate` then gives a single verdict against criteria fixed in advance
(config/eval_criteria.toml). Officer-verified farmer reports become observations automatically.

Absence records matter as much as presence records: without them false alarms cannot be
measured, and the model can look perfect simply by calling everything High.
"""
import csv
import hashlib
import random
import tomllib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
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
                "block": (r.get("block") or "").strip() or None,
                "variety": (r.get("variety") or "").strip() or None,
                "crop_stage": (r.get("crop_stage") or "").strip().lower() or None,
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
             thresholds: list[float] | None = None, weather_source: str | None = None) -> dict:
    thresholds = thresholds or list(range(30, 95, 5))
    obs = session.scalars(
        select(Observation).where(Observation.date >= start, Observation.date <= end)
    ).all()
    districts = {d.id: d for d in session.scalars(select(District))}
    daily: dict[tuple[int, date], float] = {}
    for district_id in {o.district_id for o in obs}:
        hours = merged_hours(session, district_id, weather_source)
        for day in score_series(hours, rules, districts[district_id].state):
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


# --- Pre-registered evaluation (config/eval_criteria.toml) -------------------------------------
# `backtest` above explores thresholds; `evaluate` gives one verdict against criteria fixed in
# advance, and compares the model with a baseline it must beat to be worth anything.


def load_criteria(path: Path = config.EVAL_CRITERIA_PATH) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def average_precision(scores: list[float], labels: list[int]) -> float | None:
    """Area under the precision-recall curve; ties are broken pessimistically."""
    positives = sum(labels)
    if not positives:
        return None
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], labels[i]))
    tp = fp = 0
    total = 0.0
    for i in order:
        if labels[i]:
            tp += 1
            total += tp / (tp + fp)
        else:
            fp += 1
    return total / positives


def precision_recall(pred: list[int], labels: list[int]) -> tuple[float | None, float | None]:
    tp = sum(1 for p, y in zip(pred, labels) if p and y)
    fp = sum(1 for p, y in zip(pred, labels) if p and not y)
    fn = sum(1 for p, y in zip(pred, labels) if not p and y)
    return (tp / (tp + fp) if tp + fp else None), (tp / (tp + fn) if tp + fn else None)


def in_season(day: date, baseline: dict) -> bool:
    mmdd, start, end = day.strftime("%m-%d"), baseline["season_start_mmdd"], baseline["season_end_mmdd"]
    return start <= mmdd <= end if start <= end else (mmdd >= start or mmdd <= end)


def evaluation_table(session: Session, rules: dict, crit: dict, start: date, end: date,
                     weather_source: str | None = None) -> list[dict]:
    """One row per observation with weather in its lead window: model score, baseline score, label."""
    lab, base = crit["label"], crit["baseline"]
    obs = session.scalars(select(Observation).where(Observation.date >= start, Observation.date <= end)).all()
    districts = {d.id: d for d in session.scalars(select(District))}
    scores: dict[int, dict[date, float]] = {}
    rh: dict[int, dict[date, list[float]]] = {}
    for district_id in {o.district_id for o in obs}:
        hours = merged_hours(session, district_id, weather_source)
        scores[district_id] = {d.date: d.score for d in score_series(hours, rules, districts[district_id].state)}
        by_day: dict[date, list[float]] = {}
        for h in hours:
            if h.rh_pct is not None:
                by_day.setdefault(h.ts.date(), []).append(h.rh_pct)
        rh[district_id] = by_day

    rows = []
    for o in obs:
        window = [o.date - timedelta(days=k) for k in range(lab["lead_min_days"], lab["lead_max_days"] + 1)]
        window_scores = [scores[o.district_id][d] for d in window if d in scores[o.district_id]]
        if not window_scores:
            continue
        values = [v for d in window for v in rh[o.district_id].get(d, [])]
        mean_rh = sum(values) / len(values) if values else 0.0
        model, baseline = max(window_scores), (mean_rh if in_season(o.date, base) else 0.0)
        rows.append({"district_id": o.district_id, "date": o.date, "label": int(o.blast_present),
                     "model": model, "baseline": baseline,
                     "model_alert": int(model >= rules["levels"]["high"]),
                     "baseline_alert": int(baseline >= base["alert_mean_rh_pct"])})
    return rows


def bootstrap_ap_gain(rows: list[dict], iterations: int, seed: int = 0) -> tuple[float, float] | None:
    """95 % interval of (model AP - baseline AP), resampling district-months."""
    clusters: dict[tuple, list[dict]] = {}
    for r in rows:
        clusters.setdefault((r["district_id"], r["date"].year, r["date"].month), []).append(r)
    keys = list(clusters)
    rng = random.Random(seed)
    gains = []
    for _ in range(iterations):
        sample = [r for k in (rng.choice(keys) for _ in keys) for r in clusters[k]]
        labels = [r["label"] for r in sample]
        model = average_precision([r["model"] for r in sample], labels)
        if model is None:
            continue
        gains.append(model - average_precision([r["baseline"] for r in sample], labels))
    if not gains:
        return None
    gains.sort()
    return gains[int(0.025 * len(gains))], gains[max(int(0.975 * len(gains)) - 1, 0)]


def verdict(rows: list[dict], crit: dict) -> dict:
    p = crit["pass"]
    labels = [r["label"] for r in rows]
    present, absent = sum(labels), len(labels) - sum(labels)
    districts = len({r["district_id"] for r in rows if r["label"]})
    model_ap = average_precision([r["model"] for r in rows], labels)
    base_ap = average_precision([r["baseline"] for r in rows], labels)
    mp, mr = precision_recall([r["model_alert"] for r in rows], labels)
    bp, br = precision_recall([r["baseline_alert"] for r in rows], labels)
    out = {"present": present, "absent": absent, "districts_with_present": districts,
           "model_ap": model_ap, "baseline_ap": base_ap, "model_precision": mp, "model_recall": mr,
           "baseline_precision": bp, "baseline_recall": br, "ap_gain_ci95": None}
    if present < p["min_present"] or absent < p["min_absent"] or districts < p["min_districts_with_present"]:
        out["verdict"] = "INCONCLUSIVE: too few observations or districts"
        return out
    ci = bootstrap_ap_gain(rows, p["bootstrap_iterations"])
    out["ap_gain_ci95"] = ci
    passed = (model_ap >= (base_ap or 0) * (1 + p["ap_relative_gain_min"])
              and (mp or 0) >= p["min_precision"] and (mr or 0) >= p["min_recall"]
              and (not p["require_ci_above_zero"] or (ci is not None and ci[0] > 0)))
    out["verdict"] = ("PASS: worth a supervised pilot, not validated" if passed
                      else "FAIL: do not build more on this rule")
    return out
