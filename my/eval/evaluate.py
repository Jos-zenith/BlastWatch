"""Backtest the rule model against a calendar baseline on labelled outbreaks.

    python eval/evaluate.py --labels eval/labels.csv

Needs internet (Open-Meteo archive, cached under eval/cache). Uses reanalysis, not archived
forecasts, so results are OPTIMISTIC about real-time skill. Treat a pass as "worth a pilot",
never as validation."""
import argparse
import csv
import hashlib
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
from blastwatch import risk, weather  # noqa: E402
from blastwatch.config import load_blocks, load_params  # noqa: E402


def average_precision(scores: list[float], labels: list[int]) -> float:
    """Area under the precision-recall curve as average precision (ties broken pessimistically)."""
    pos = sum(labels)
    if pos == 0:
        return float("nan")
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], labels[i]))
    tp = fp = 0
    ap = 0.0
    for i in order:
        if labels[i]:
            tp += 1
            ap += tp / (tp + fp)
        else:
            fp += 1
    return ap / pos


def prec_recall(pred: list[int], labels: list[int]) -> tuple[float, float]:
    tp = sum(1 for p, y in zip(pred, labels) if p and y)
    fp = sum(1 for p, y in zip(pred, labels) if p and not y)
    fn = sum(1 for p, y in zip(pred, labels) if not p and y)
    return (tp / (tp + fp) if tp + fp else 0.0), (tp / (tp + fn) if tp + fn else 0.0)


def in_window(d: date, c: dict) -> bool:
    mmdd = d.strftime("%m-%d")
    return c["window_start_mmdd"] <= mmdd <= c["window_end_mmdd"]


def build_table(hourly: dict[str, list[dict]], onsets: dict[str, list[date]], p: dict, crit: dict,
                start: date, end: date) -> list[dict]:
    """One row per block-day with model score, baseline score, and label."""
    L, B = crit["label"], crit["baseline"]
    win = p["alert"]["window_days"]
    table = []
    for block, rows in hourly.items():
        dh = risk.daily_favorable_hours(rows, p)
        rh_by_day: dict[str, list[float]] = {}
        for r in rows:
            if r["rh"] is not None:
                rh_by_day.setdefault(r["ts"][:10], []).append(r["rh"])
        d = start
        while d <= end - timedelta(days=L["lead_max_days"]):
            lvl = risk.block_level(dh, d, p)
            if lvl["level"] != "NO_DATA":
                model = risk.continuous_score(dh, d, win)
                vals = [v for k in range(7) for v in rh_by_day.get((d + timedelta(k)).isoformat(), [])]
                weekly_rh = sum(vals) / len(vals) if vals else 0.0
                base = weekly_rh if in_window(d, B) else 0.0
                label = int(any(d + timedelta(L["lead_min_days"]) <= o <= d + timedelta(L["lead_max_days"])
                                for o in onsets.get(block, [])))
                table.append({"block": block, "day": d, "model": model, "base": base, "label": label,
                              "model_alert": int(lvl["level"] == "HIGH"),
                              "base_alert": int(base >= B["weekly_mean_rh_min"])})
            d += timedelta(days=1)
    return table


def bootstrap_ap_diff(table: list[dict], iters: int, seed: int = 0) -> tuple[float, float]:
    clusters: dict[tuple, list[dict]] = {}
    for r in table:
        clusters.setdefault((r["block"], r["day"].year, r["day"].month), []).append(r)
    keys = list(clusters)
    rng = random.Random(seed)
    diffs = []
    for _ in range(iters):
        sample = [r for k in (rng.choice(keys) for _ in keys) for r in clusters[k]]
        y = [r["label"] for r in sample]
        if sum(y) == 0:
            continue
        diffs.append(average_precision([r["model"] for r in sample], y) -
                     average_precision([r["base"] for r in sample], y))
    diffs.sort()
    if not diffs:
        return float("nan"), float("nan")
    return diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs)) - 1]


def verdict(table: list[dict], n_events: int, n_blocks: int, crit: dict, ci: tuple[float, float]) -> dict:
    P = crit["pass"]
    y = [r["label"] for r in table]
    ap_m = average_precision([r["model"] for r in table], y)
    ap_b = average_precision([r["base"] for r in table], y)
    pm, rm = prec_recall([r["model_alert"] for r in table], y)
    pb, rb = prec_recall([r["base_alert"] for r in table], y)
    out = {"events": n_events, "blocks_with_events": n_blocks, "model_AP": ap_m, "baseline_AP": ap_b,
           "prevalence": sum(y) / len(y) if y else 0, "model_precision": pm, "model_recall": rm,
           "baseline_precision": pb, "baseline_recall": rb, "AP_diff_CI95": ci}
    if n_events < P["min_positive_events"] or n_blocks < P["min_blocks_with_events"]:
        out["verdict"] = "INCONCLUSIVE (too few events/blocks)"
        return out
    ok = (ap_m >= ap_b * (1 + P["ap_relative_gain_min"]) and pm >= P["min_precision_at_alert"]
          and rm >= P["min_recall_at_alert"] and (not P["bootstrap_ci_excludes_zero"] or ci[0] > 0))
    out["verdict"] = "PASS (worth piloting; not validated)" if ok else "FAIL (do not build more on this rule)"
    return out


def load_labels(path: Path) -> dict[str, list[date]]:
    onsets: dict[str, list[date]] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if not r.get("definition", "").strip():
                continue
            try:
                onsets.setdefault(r["block_id"], []).append(date.fromisoformat(r["onset_date"]))
            except ValueError:
                continue
    return onsets


def cached_archive(block: dict, start: str, end: str) -> list[dict]:
    cache = HERE / "cache"
    cache.mkdir(exist_ok=True)
    f = cache / f"{block['block_id']}_{start}_{end}.json"
    if f.exists():
        return json.loads(f.read_text())
    rows = weather.fetch_archive(block["lat"], block["lon"], start, end)
    f.write_text(json.dumps(rows))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--criteria", type=Path, default=HERE / "criteria.yaml")
    a = ap.parse_args()
    crit = yaml.safe_load(a.criteria.read_text(encoding="utf-8"))
    p = load_params()
    print("criteria sha256:", hashlib.sha256(a.criteria.read_bytes()).hexdigest()[:12],
          "| params sha256:", hashlib.sha256((HERE.parent / "config/params.yaml").read_bytes()).hexdigest()[:12])
    onsets = load_labels(a.labels)
    s, e = crit["eval_period"]["start"], crit["eval_period"]["end"]
    blocks = [b for b in load_blocks() if b["block_id"] in onsets]
    hourly = {b["block_id"]: cached_archive(b, s, e) for b in blocks}
    table = build_table(hourly, onsets, p, crit, date.fromisoformat(s), date.fromisoformat(e))
    if not table:
        print("no block-days; check labels/blocks")
        return 1
    n_events = sum(len(v) for v in onsets.values())
    ci = bootstrap_ap_diff(table, crit["pass"]["bootstrap_iterations"])
    print(json.dumps(verdict(table, n_events, len(onsets), crit, ci), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
