"""Daily job: fetch -> risk -> block alert -> farmer messages -> follow-ups.
Run once or twice a day from cron / Windows Task Scheduler:  python -m blastwatch run"""
import hashlib
import json
from datetime import date, datetime, timedelta, timezone

from . import notify, risk, stage, weather
from .config import load_blocks, load_chemicals, load_params, load_varieties, params_hash


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sampled(sub_id: int, day: date, fraction: float) -> bool:
    h = hashlib.sha256(f"{sub_id}:{day}".encode()).digest()[0] / 255
    return h < fraction


def compose_alert(sub: dict, st: dict, blk: dict, lvl: dict, p: dict, variety: dict, chem: dict) -> str:
    lang = sub["language"] if sub["language"] in chem["advice"]["high"] else "en"
    lines = [
        f"BlastWatch (pilot) - {blk['block']}, {blk['district']}",
        f"Humid, cool weather favourable for blast expected {lvl['window_start']} to {lvl['window_end']}.",
        f"Your crop: {sub['variety']}, stage: {st['stage']}.",
    ]
    if variety.get("blast_rating") == "susceptible":
        lines.append("This variety is known to be blast-susceptible.")
    lines.append(chem["advice"]["high"][lang])
    lines.append(chem["advice"]["chemical_block"] if not chem.get("reviewed") else "")
    lines.append("This is a weather-based warning, not a diagnosis. Weather is a regional estimate.")
    return "\n".join(x for x in lines if x)


def compose_followup(sub: dict, origin: str) -> str:
    return ("BlastWatch check-in: in the last 7 days, did you see diamond/eye-shaped spots with grey "
            "centres on the leaves, or a blackened neck at the ear? Reply YES, NO or NOT SURE. "
            "If YES, your agriculture officer may visit to confirm.")


def run_daily(con, today: date | None = None, fetch=weather.fetch_forecast, p: dict | None = None) -> dict:
    today = today or date.today()
    p = p or load_params()
    ph = params_hash()
    chem, vars_ = load_chemicals(), load_varieties()
    summary = {"blocks": 0, "high": 0, "no_data": 0, "alerts_sent": 0, "followups_sent": 0, "errors": []}

    for blk in load_blocks():
        try:
            rows = fetch(blk["lat"], blk["lon"], p["weather"]["forecast_days"])
            weather.store(con, blk["block_id"], rows, "open-meteo-forecast")
        except Exception as e:  # network failure must not kill other blocks
            summary["errors"].append(f"{blk['block_id']}: {type(e).__name__}")
            continue
        lvl = risk.block_level(risk.daily_favorable_hours(rows, p), today, p)
        summary["blocks"] += 1
        con.execute(
            "INSERT OR REPLACE INTO block_alerts(block_id,issued_on,level,window_start,window_end,"
            "favorable_days,hours_json,params_hash) VALUES(?,?,?,?,?,?,?,?)",
            (blk["block_id"], today.isoformat(), lvl["level"], lvl["window_start"], lvl["window_end"],
             lvl["favorable_days"], json.dumps(lvl["hours"]), ph))
        alert_id = con.execute("SELECT id FROM block_alerts WHERE block_id=? AND issued_on=?",
                               (blk["block_id"], today.isoformat())).fetchone()["id"]
        con.commit()
        summary["no_data"] += lvl["level"] == "NO_DATA"
        subs = [dict(r) for r in con.execute(
            "SELECT * FROM subscribers WHERE block_id=? AND active=1", (blk["block_id"],))]

        if lvl["level"] == "HIGH":
            summary["high"] += 1
            for sub in subs:
                st = stage.stage_for(sub, today, p, vars_)
                if not st["susceptible"]:
                    continue
                recent = con.execute(
                    "SELECT 1 FROM messages WHERE subscriber_id=? AND kind='alert' AND sent_at>=?",
                    (sub["id"], (today - timedelta(days=p["alert"]["cooldown_days"])).isoformat())).fetchone()
                if recent:
                    continue
                body = compose_alert(sub, st, blk, lvl, p, vars_.get(sub["variety"], vars_["Other/Unknown"]), chem)
                status = notify.send(sub["channel"], sub["contact"], body)
                con.execute("INSERT INTO messages(subscriber_id,kind,body,sent_at,status,block_alert_id)"
                            " VALUES(?,?,?,?,?,?)", (sub["id"], "alert", body, _now(), status, alert_id))
                for d in p["feedback"]["followup_days"]:
                    con.execute("INSERT INTO followups(subscriber_id,due_on,origin,block_alert_id)"
                                " VALUES(?,?,?,?)", (sub["id"], (today + timedelta(days=d)).isoformat(),
                                                     "alert", alert_id))
                summary["alerts_sent"] += 1
        else:
            # Sample no-alert subscribers too, otherwise missed outbreaks are never learned.
            for sub in subs:
                if stage.stage_for(sub, today, p, vars_)["susceptible"] and _sampled(
                        sub["id"], today, p["feedback"]["no_alert_checkin_fraction"] / 7):
                    con.execute("INSERT INTO followups(subscriber_id,due_on,origin,block_alert_id)"
                                " VALUES(?,?,?,?)", (sub["id"], (today + timedelta(days=7)).isoformat(),
                                                     "no_alert_sample", alert_id))
        con.commit()

    summary["followups_sent"] = send_due_followups(con, today)
    return summary


def send_due_followups(con, today: date) -> int:
    n = 0
    for f in con.execute("SELECT f.*, s.channel, s.contact, s.id AS sid FROM followups f"
                         " JOIN subscribers s ON s.id=f.subscriber_id"
                         " WHERE f.asked_at IS NULL AND f.due_on<=? AND s.active=1", (today.isoformat(),)).fetchall():
        # skip if the farmer already has an unanswered question open (avoid spam)
        open_q = con.execute("SELECT 1 FROM followups WHERE subscriber_id=? AND asked_at IS NOT NULL"
                             " AND answered_at IS NULL AND id<>?", (f["sid"], f["id"])).fetchone()
        if open_q:
            continue
        body = compose_followup({}, f["origin"])
        status = notify.send(f["channel"], f["contact"], body)
        con.execute("UPDATE followups SET asked_at=? WHERE id=?", (_now(), f["id"]))
        con.execute("INSERT INTO messages(subscriber_id,kind,body,sent_at,status,block_alert_id)"
                    " VALUES(?,?,?,?,?,?)", (f["sid"], "followup", body, _now(), status, f["block_alert_id"]))
        n += 1
    con.commit()
    return n
