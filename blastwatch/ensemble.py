"""Ensemble risk: how likely is a High-risk night, given forecast uncertainty?

The deterministic forecast gives one answer per night, but leaf wetness is exactly where weather
models disagree (43-87 % leaf-wetness probability for the same hour at Thanjavur across ECMWF,
GFS, ICON and JMA). Open-Meteo's Ensemble API returns every member of three global ensembles:

    ECMWF IFS ENS 0.25°   51 members
    NCEP GEFS 0.25°       31 members
    DWD ICON EPS          40 members

Each member's hourly series is scored with the same rules engine as the live forecast, so a
member is one plausible version of the coming nights and P(High) is the share of members whose
night scores High. The three ensembles are weighted equally (not by member count), so ECMWF's 51
members do not outvote the others, and per-model probabilities are kept so disagreement between
centres is visible.

This adds a probability, not a validation: whether a 70 % P(High) night is followed by blast more
often than a 30 % one still needs field observations (see Calibration). The ensemble does not
change who is alerted; `ensemble_action` is reported next to the rule-based action for comparison.
"""
import logging
import statistics
import time
from collections import defaultdict
from datetime import date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import upsert
from .events import publish
from .ingest.weather import HOURLY_VARS, OPEN_METEO_CALLS_PER_MIN, OPEN_METEO_MAX_LOCATIONS, _get_json
from .models import District, EnsembleDaily
from .risk import HourObs, score_series
from .runs import track

log = logging.getLogger("blastwatch.ensemble")

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
# Short key (stored, max 10 chars) -> Open-Meteo model id, label, member count incl. control.
MODELS = {
    "ecmwf": ("ecmwf_ifs025", "ECMWF IFS ENS", 51),
    "gefs": ("gfs025", "NCEP GEFS", 31),
    "icon": ("icon_seamless", "DWD ICON EPS", 40),
}
POOLED = "all"
# Open-Meteo does not document ensemble billing. Its docs price a 1-variable, 1-location ICON EPS
# (40 members) request at 4.0 calls, which fits members / 10 on top of the usual weighting. We pace on
# that assumption: all three models for 18 districts ~ 220 calls per refresh.
MEMBERS_PER_CALL = 10


def ensemble_weight(n_locations: int, members: int, n_vars: int) -> float:
    return n_locations * max(1.0, members / MEMBERS_PER_CALL) * max(1.0, n_vars / 10)


def parse_members(payload: dict, now: datetime) -> dict[str, list[HourObs]]:
    """{member: hours}. The unsuffixed column is the control run ("00"), then member01..NN."""
    h = payload["hourly"]
    times = [datetime.fromisoformat(t) for t in h["time"]]
    n = len(times)
    suffixes = [""] + sorted({k.rsplit("_member", 1)[1] for k in h if "_member" in k})

    def col(var: str, suffix: str):
        return h.get(var + (f"_member{suffix}" if suffix else "")) or [None] * n

    members = {}
    for suffix in suffixes:
        temp, rh, dew = col("temperature_2m", suffix), col("relative_humidity_2m", suffix), col("dew_point_2m", suffix)
        rain, cloud, lwp = col("precipitation", suffix), col("cloud_cover", suffix), col("leaf_wetness_probability", suffix)
        if all(v is None for v in temp):
            continue
        members[suffix or "00"] = [
            HourObs(ts, temp[i], rh[i], rain[i], cloud[i], ts > now, dew[i], lwp[i])
            for i, ts in enumerate(times) if temp[i] is not None
        ]
    return members


def quantile(values: list[float], q: float) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[round(q * 100) - 1]


def summarise(scores: list[float], levels: list[str], wet_hours: list[int]) -> dict:
    n = len(scores)
    return {
        "members": n,
        "p_high": round(levels.count("High") / n, 3),
        "p_moderate": round(levels.count("Moderate") / n, 3),
        "score_p10": round(quantile(scores, 0.10), 1),
        "score_p50": round(quantile(scores, 0.50), 1),
        "score_p90": round(quantile(scores, 0.90), 1),
        "score_mean": round(sum(scores) / n, 1),
        "wet_hours_p50": round(quantile(wet_hours, 0.50), 1),
    }


def score_members(members: dict[str, list[HourObs]], rules: dict, state: str, today: date) -> dict[date, dict]:
    """Per forecast date: every member's score, level and wet hours."""
    by_day: dict[date, dict[str, list]] = defaultdict(lambda: {"scores": [], "levels": [], "wet": []})
    for hours in members.values():
        for day in score_series(hours, rules, state):
            if day.date < today:
                continue
            bucket = by_day[day.date]
            bucket["scores"].append(day.score)
            bucket["levels"].append(day.level)
            bucket["wet"].append(day.wet_hours)
    return by_day


def pooled(per_model: dict[str, dict]) -> dict:
    """Equal weight per ensemble: probabilities are averaged across models; the score spread is
    taken from the model medians and extremes so it still shows the full disagreement."""
    rows = list(per_model.values())
    return {
        "members": sum(r["members"] for r in rows),
        "p_high": round(sum(r["p_high"] for r in rows) / len(rows), 3),
        "p_moderate": round(sum(r["p_moderate"] for r in rows) / len(rows), 3),
        "score_p10": min(r["score_p10"] for r in rows),
        "score_p50": round(statistics.median(r["score_p50"] for r in rows), 1),
        "score_p90": max(r["score_p90"] for r in rows),
        "score_mean": round(sum(r["score_mean"] for r in rows) / len(rows), 1),
        "wet_hours_p50": round(statistics.median(r["wet_hours_p50"] for r in rows), 1),
    }


def fetch_model(districts: list[District], model: str, client: httpx.Client) -> list[dict]:
    """Raw payload per district, one multi-location request per OPEN_METEO_MAX_LOCATIONS."""
    model_id, _, _ = MODELS[model]
    payloads = []
    for start in range(0, len(districts), OPEN_METEO_MAX_LOCATIONS):
        chunk = districts[start:start + OPEN_METEO_MAX_LOCATIONS]
        params = {
            "latitude": ",".join(str(d.lat) for d in chunk),
            "longitude": ",".join(str(d.lon) for d in chunk),
            "hourly": HOURLY_VARS,
            "models": model_id,
            "timezone": config.TIMEZONE,
            "past_days": 1,  # today's infection night started yesterday at noon
            "forecast_days": 7,
        }
        payload = _get_json(ENSEMBLE_URL, params, client)
        payloads.extend(payload if isinstance(payload, list) else [payload])
    return payloads


def ingest(session: Session, rules: dict, models: list[str] | None = None, client: httpx.Client | None = None,
           pace: bool = True) -> dict:
    """Fetch, score and store every model; a failing model is logged and skipped."""
    if client is None:
        with httpx.Client(timeout=max(config.HTTP_TIMEOUT, 120)) as own:
            return ingest(session, rules, models, own, pace)
    models = models or rules.get("ensemble", {}).get("models", list(MODELS))
    districts = session.scalars(select(District).order_by(District.id)).all()
    now, today = datetime.now(), date.today()
    version = rules["model_version"]
    n_vars = len(HOURLY_VARS.split(","))
    summaries: dict[tuple[int, date], dict[str, dict]] = defaultdict(dict)
    done = []
    for i, model in enumerate(models):
        if i and pace:
            spent = ensemble_weight(len(districts), MODELS[models[i - 1]][2], n_vars)
            time.sleep(60 * spent / OPEN_METEO_CALLS_PER_MIN)
        try:
            with track(session, f"ens-{model}") as run:
                payloads = fetch_model(districts, model, client)
                for district, payload in zip(districts, payloads, strict=True):
                    members = parse_members(payload, now)
                    for day, b in score_members(members, rules, district.state, today).items():
                        summaries[(district.id, day)][model] = summarise(b["scores"], b["levels"], b["wet"])
                run.rows = len(payloads)
            done.append(model)
            publish("ensemble.model", model=model, label=MODELS[model][1], members=MODELS[model][2])
        except Exception:
            log.exception("ensemble model %s failed", model)
            publish("ensemble.failed", model=model, label=MODELS[model][1])

    rows = []
    for (district_id, day), per_model in summaries.items():
        for model, s in [*per_model.items(), (POOLED, pooled(per_model))]:
            rows.append({"district_id": district_id, "date": day, "model": model, "model_version": version,
                         "run_at": now, **s})
    upsert(session, EnsembleDaily, rows, ["district_id", "date", "model", "model_version"])
    session.commit()
    result = {"models": done, "rows": len(rows)}
    if done:
        publish("ensemble.updated", **result)
    return result


def ensemble_action(days: list[dict], rules: dict, today: date) -> str | None:
    """Experimental rule on the pooled probabilities, for comparison with the rule-based action:
    alert when at least `min_high_days` of the near-horizon days have P(High) >= alert_p."""
    if not days:
        return None
    a, e = rules["alerts"], rules.get("ensemble", {})
    near = [d for d in days if (d["date"] - today).days < a["horizon_days"]]
    likely = sum(d["p_high"] >= e.get("alert_p", 0.5) for d in near)
    if likely >= a["min_high_days"]:
        return "alert"
    if any(d["p_high"] + d["p_moderate"] >= 0.5 or d["p_high"] >= 0.2 for d in days):
        return "watch"
    return "none"


def forecast_confidence(level: str, p_high: float | None, p_moderate: float | None, lead_days: int,
                        fresh_status: str, rules: dict) -> dict:
    """How far the weather forecast behind a level can be trusted: "High", "Moderate", "Low", or
    None when no ensemble covers the night. It is the share of pooled ensemble members that score
    the same level, capped by data age and lead time. It says nothing about whether blast follows:
    that needs observations (see Calibration)."""
    c = rules.get("confidence", {})
    if fresh_status != "fresh":
        return {"level": "Low", "reason": "stale"}
    if lead_days >= rules["alerts"]["horizon_days"]:
        return {"level": "Low", "reason": "lead"}
    if p_high is None or p_moderate is None:
        return {"level": None, "reason": "no_ensemble"}
    agree = {"High": p_high, "Moderate": p_moderate}.get(level, 1.0 - p_high - p_moderate)
    tier = ("High" if agree >= c.get("high_agree", 0.7)
            else "Moderate" if agree >= c.get("moderate_agree", 0.4) else "Low")
    return {"level": tier, "reason": "agreement", "agree": round(agree, 2)}
