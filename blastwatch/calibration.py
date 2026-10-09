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

Both commands judge the same thing: whether the alert rule, replayed day by day as the live system
runs it (alerts.evaluate), warned of an observation within the same label window. `backtest` may
only see observations before the holdout boundary and `evaluate` only those after it, so the
thresholds are never tuned on the data that judges them.

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
from .risk import score_series, window_date

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


def load_criteria(path: Path = config.EVAL_CRITERIA_PATH) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


# --- Tuning / holdout split (config/eval_criteria.toml [split]) -----------------------------------


class SplitError(ValueError):
    """A date range that reaches across the tuning / holdout boundary."""


def split_dates(crit: dict) -> tuple[date, date]:
    """(holdout_start, evaluate_from). Observations before holdout_start may tune the rules; those
    from evaluate_from on are the holdout. The gap between them is lead_max_days long, so no night of
    weather that labels a tuning observation also labels a holdout one. The gap is used by neither."""
    holdout = date.fromisoformat(crit["split"]["holdout_start"])
    return holdout, holdout + timedelta(days=crit["label"]["lead_max_days"])


def check_range(crit: dict, start: date, end: date, purpose: str) -> None:
    """Refuse, rather than trim, a range that is not wholly on `purpose`'s side of the boundary:
    a silently trimmed range would report fewer observations than were asked for."""
    if start > end:
        raise SplitError(f"--start {start} is after --end {end}")
    holdout, evaluate_from = split_dates(crit)
    where = "config/eval_criteria.toml [split]"
    if purpose == "tune" and end >= holdout:
        raise SplitError(
            f"backtest tunes the rules, so it may only use observations before the holdout ({holdout}, "
            f"{where}). --end {end} reaches it; use --end {holdout - timedelta(days=1)} or earlier.")
    if purpose == "evaluate" and start < evaluate_from:
        raise SplitError(
            f"evaluate may only use held-out observations, from {evaluate_from} on ({where}: the holdout "
            f"starts {holdout}, and the first {crit['label']['lead_max_days']} days are a gap whose weather "
            f"overlaps the tuning period). --start {start} is earlier; use --start {evaluate_from} or later.")


# --- The alert rule, replayed --------------------------------------------------------------------


def label_window(day: date, crit: dict) -> list[date]:
    """The nights that could have caused what was seen on `day`: lesions show days after infection.
    A warning counts only if it named one of these nights, so a warning too late to act on (fewer
    than lead_min_days ahead) or too early to be related (more than lead_max_days) never counts."""
    lab = crit["label"]
    return [day - timedelta(days=k) for k in range(lab["lead_min_days"], lab["lead_max_days"] + 1)]


def replay_alerts(high: set[date], rules: dict) -> list[tuple[date, list[date]]]:
    """The alerts the live rule (alerts.evaluate, district headquarters) would have issued for one
    district with these High nights, as (issued_on, high_days) in date order. Each day the rule looks
    `horizon_days` ahead and alerts on at least `min_high_days` High nights, unless the district's
    latest alert began within `cooldown_days` of this episode's first High night.

    Replayed on one archived forecast per night at a fixed lead, an alert sees every night of its
    horizon at that lead, while the live rule sees nearer nights at shorter leads."""
    a = rules["alerts"]
    if not high:
        return []
    issued, last_start = [], None
    day, end = min(high) - timedelta(days=a["horizon_days"] - 1), max(high)
    while day <= end:
        days = [d for d in (day + timedelta(days=k) for k in range(a["horizon_days"])) if d in high]
        if len(days) >= a["min_high_days"] and (
                last_start is None or last_start < days[0] - timedelta(days=a["cooldown_days"])):
            issued.append((day, days))
            last_start = days[0]
        day += timedelta(days=1)
    return issued


def alerted_nights(scores: dict[date, float], threshold: float, rules: dict) -> set[date]:
    """Nights named in a replayed alert when High starts at `threshold`."""
    high = {d for d, s in scores.items() if s >= threshold}
    return {d for _, days in replay_alerts(high, rules) for d in days}


def district_weather(session: Session, rules: dict, district_ids: set[int], weather_source: str | None
                     ) -> tuple[dict[int, dict[date, float]], dict[int, dict[date, list[float]]]]:
    """Per district: the daily risk score and the hourly RH values, both keyed by infection night."""
    districts = {d.id: d for d in session.scalars(select(District))}
    start_hour = rules.get("window_start_hour", 12)
    scores, rh = {}, {}
    for district_id in district_ids:
        hours = merged_hours(session, district_id, weather_source)
        scores[district_id] = {d.date: d.score for d in score_series(hours, rules, districts[district_id].state)}
        by_night: dict[date, list[float]] = {}
        for h in hours:
            if h.rh_pct is not None:
                by_night.setdefault(window_date(h.ts, start_hour), []).append(h.rh_pct)
        rh[district_id] = by_night
    return scores, rh


# --- Backtest: explore thresholds on the tuning period ---------------------------------------------


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
    scores: dict[int, dict[date, float]],
    alerted: dict[int, set[date]],
    crit: dict,
    threshold: float,
) -> tuple[Scores, int]:
    """An observation is "warned" if a replayed alert named a night in its label window.
    Observations with no scored night in that window are skipped."""
    tp = fp = fn = tn = skipped = 0
    for district_id, day, present in observations:
        window = label_window(day, crit)
        if not any(d in scores.get(district_id, {}) for d in window):
            skipped += 1
            continue
        warned = any(d in alerted.get(district_id, ()) for d in window)
        if present and warned:
            tp += 1
        elif present:
            fn += 1
        elif warned:
            fp += 1
        else:
            tn += 1
    return Scores(threshold, tp, fp, fn, tn), skipped


def backtest(session: Session, rules: dict, crit: dict, start: date, end: date,
             thresholds: list[float] | None = None, weather_source: str | None = None) -> dict:
    check_range(crit, start, end, "tune")
    thresholds = thresholds or list(range(30, 95, 5))
    obs = session.scalars(
        select(Observation).where(Observation.date >= start, Observation.date <= end)
    ).all()
    scores, _ = district_weather(session, rules, {o.district_id for o in obs}, weather_source)
    pairs = [(o.district_id, o.date, o.blast_present) for o in obs]
    results = [contingency(pairs, scores, {k: alerted_nights(v, t, rules) for k, v in scores.items()}, crit, t)
               for t in thresholds]
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
                     weather_source: str | None = None) -> tuple[list[dict], int]:
    """One row per held-out observation with weather in its label window, and how many were skipped
    for having none. Model and baseline are summarised the same way, as the mean over the window
    (daily score; hourly RH), so neither gets the advantage of a peak over a mean. The model's alert
    is the alert rule replayed as it runs live; the baseline alerts on its pre-registered RH cutoff."""
    check_range(crit, start, end, "evaluate")
    base = crit["baseline"]
    obs = session.scalars(select(Observation).where(Observation.date >= start, Observation.date <= end)).all()
    scores, rh = district_weather(session, rules, {o.district_id for o in obs}, weather_source)
    alerted = {k: alerted_nights(v, rules["levels"]["high"], rules) for k, v in scores.items()}

    rows, skipped = [], 0
    for o in obs:
        window = label_window(o.date, crit)
        window_scores = [scores[o.district_id][d] for d in window if d in scores[o.district_id]]
        if not window_scores:
            skipped += 1
            continue
        values = [v for d in window for v in rh[o.district_id].get(d, [])]
        mean_rh = sum(values) / len(values) if values else 0.0
        baseline = mean_rh if in_season(o.date, base) else 0.0
        rows.append({"district_id": o.district_id, "date": o.date, "label": int(o.blast_present),
                     "model": sum(window_scores) / len(window_scores), "baseline": baseline,
                     "model_alert": int(any(d in alerted[o.district_id] for d in window)),
                     "baseline_alert": int(baseline >= base["alert_mean_rh_pct"])})
    return rows, skipped


def lift(pred: list[int], labels: list[int]) -> float | None:
    """Precision over the share of presences in the sample: 1.0 is what alerting at random gives.
    Precision alone depends on how many absences happened to be collected; lift does not."""
    precision, _ = precision_recall(pred, labels)
    prevalence = sum(labels) / len(labels) if labels else 0
    if not prevalence:
        return None
    return (precision or 0.0) / prevalence  # never alerting is as useless as alerting at random, or worse


def bootstrap_ci(rows: list[dict], iterations: int, seed: int = 0) -> dict:
    """95 % intervals of (model AP - baseline AP) and of the model's lift, resampling district-months:
    days in one month are correlated, so resampling single rows would overstate the certainty."""
    clusters: dict[tuple, list[dict]] = {}
    for r in rows:
        clusters.setdefault((r["district_id"], r["date"].year, r["date"].month), []).append(r)
    keys = list(clusters)
    rng = random.Random(seed)
    gains, lifts = [], []
    for _ in range(iterations):
        sample = [r for k in (rng.choice(keys) for _ in keys) for r in clusters[k]]
        labels = [r["label"] for r in sample]
        model = average_precision([r["model"] for r in sample], labels)
        if model is None:
            continue
        gains.append(model - average_precision([r["baseline"] for r in sample], labels))
        lifts.append(lift([r["model_alert"] for r in sample], labels))

    def interval(values):
        if not values:
            return None
        values.sort()
        return values[int(0.025 * len(values))], values[max(int(0.975 * len(values)) - 1, 0)]

    return {"ap_gain": interval(gains), "lift": interval(lifts)}


def verdict(rows: list[dict], crit: dict) -> dict:
    """INCONCLUSIVE when the sample is too small, FAIL when the point estimates miss the criteria,
    and INCONCLUSIVE again when they pass but the 95 % intervals still include no gain over the
    baseline or no better than chance: a small, noisy pass is not evidence."""
    p = crit["pass"]
    labels = [r["label"] for r in rows]
    present, absent = sum(labels), len(labels) - sum(labels)
    districts = len({r["district_id"] for r in rows if r["label"]})
    model_ap = average_precision([r["model"] for r in rows], labels)
    base_ap = average_precision([r["baseline"] for r in rows], labels)
    mp, mr = precision_recall([r["model_alert"] for r in rows], labels)
    bp, br = precision_recall([r["baseline_alert"] for r in rows], labels)
    out = {"present": present, "absent": absent, "districts_with_present": districts,
           "prevalence": present / len(rows) if rows else None,
           "model_alert_rate": sum(r["model_alert"] for r in rows) / len(rows) if rows else None,
           "model_ap": model_ap, "baseline_ap": base_ap, "model_precision": mp, "model_recall": mr,
           "model_lift": lift([r["model_alert"] for r in rows], labels),
           "baseline_precision": bp, "baseline_recall": br,
           "baseline_lift": lift([r["baseline_alert"] for r in rows], labels),
           "ap_gain_ci95": None, "lift_ci95": None}
    if present < p["min_present"] or absent < p["min_absent"] or districts < p["min_districts_with_present"]:
        out["verdict"] = "INCONCLUSIVE: too few observations or districts"
        return out
    ci = bootstrap_ci(rows, p["bootstrap_iterations"])
    out["ap_gain_ci95"], out["lift_ci95"] = ci["ap_gain"], ci["lift"]
    meets = (model_ap >= (base_ap or 0) * (1 + p["ap_relative_gain_min"])
             and (out["model_lift"] or 0) >= p["min_precision_lift"] and (mr or 0) >= p["min_recall"])
    if not meets:
        out["verdict"] = "FAIL: do not build more on this rule"
    elif ci["ap_gain"] is None or ci["ap_gain"][0] <= 0 or ci["lift"] is None or ci["lift"][0] <= 1:
        out["verdict"] = ("INCONCLUSIVE: the estimates pass, but the 95 % intervals include no gain over "
                          "the baseline or no better than chance; collect more held-out observations")
    else:
        out["verdict"] = "PASS: worth a supervised pilot, not validated"
    return out
