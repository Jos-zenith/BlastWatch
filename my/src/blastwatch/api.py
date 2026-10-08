"""Small FastAPI surface: enrol (with consent), farmer reply, officer verification, status."""
import os
import sqlite3
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import stage as stage_mod
from .config import load_blocks, load_params, load_varieties
from .db import connect


def get_con():
    con = connect()
    try:
        yield con
    finally:
        con.close()

CONSENT_VERSION = "v0-draft"
CONSENT_TEXT = ("BlastWatch is a pilot run by <SPONSOR - to be named>. We will message you weather-based "
                "rice-blast warnings and ask whether you saw symptoms. We store your phone/chat id, block, "
                "variety and sowing/transplanting date only to do this. Reply STOP to leave. "
                "Warnings are advisory, not a diagnosis.")

app = FastAPI(title="BlastWatch pilot", version="0.1.0")


def officer_auth(x_officer_key: str = Header(default="")):
    key = os.environ.get("BLASTWATCH_OFFICER_KEY")
    if not key or x_officer_key != key:
        raise HTTPException(401, "officer key required (set BLASTWATCH_OFFICER_KEY)")


class Enrol(BaseModel):
    contact: str
    channel: Literal["outbox", "telegram"] = "outbox"
    block_id: str
    variety: str
    method: Literal["transplanted", "direct_seeded"]
    establish_date: date
    language: Literal["ta", "en"] = "ta"
    consent: bool


class Reply(BaseModel):
    contact: str
    answer: Literal["yes", "no", "unsure"]


class Verify(BaseModel):
    followup_id: int
    status: Literal["confirmed", "rejected"]
    note: str = ""


@app.get("/consent")
def consent():
    return {"version": CONSENT_VERSION, "text": CONSENT_TEXT}


@app.post("/enrol")
def enrol(e: Enrol, con=Depends(get_con)):
    if not e.consent:
        raise HTTPException(400, "consent required")
    if e.block_id not in {b["block_id"] for b in load_blocks()}:
        raise HTTPException(400, "unknown block_id")
    if e.establish_date > date.today():
        raise HTTPException(400, "establish_date is in the future")
    if (date.today() - e.establish_date).days > 200:
        raise HTTPException(400, "establish_date implausibly old")
    variety = e.variety if e.variety in load_varieties() else "Other/Unknown"
    try:
        con.execute(
            "INSERT INTO subscribers(contact,channel,block_id,variety,method,establish_date,language,"
            "consent_at,consent_text_version) VALUES(?,?,?,?,?,?,?,?,?)",
            (e.contact, e.channel, e.block_id, variety, e.method, e.establish_date.isoformat(), e.language,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), CONSENT_VERSION))
        con.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "already enrolled")
    return {"ok": True, "variety_recorded_as": variety}


@app.post("/stop/{contact}")
def stop(contact: str, con=Depends(get_con)):
    con.execute("UPDATE subscribers SET active=0 WHERE contact=?", (contact,))
    con.commit()
    return {"ok": True}


@app.post("/reply")
def reply(r: Reply, con=Depends(get_con)):
    f = con.execute(
        "SELECT f.id FROM followups f JOIN subscribers s ON s.id=f.subscriber_id"
        " WHERE s.contact=? AND f.asked_at IS NOT NULL AND f.answered_at IS NULL ORDER BY f.id LIMIT 1",
        (r.contact,)).fetchone()
    if not f:
        raise HTTPException(404, "no open question")
    con.execute("UPDATE followups SET answer=?, answered_at=? WHERE id=?",
                (r.answer, datetime.now(timezone.utc).isoformat(timespec="seconds"), f["id"]))
    con.commit()
    return {"ok": True, "note": "Thanks. A yes is only a report; an officer verifies before anything is confirmed."}


@app.get("/status")
def status(con=Depends(get_con)):
    """Block-level view for officers: latest level per block, subscribers in susceptible stage,
    and unverified farmer reports. (Deliberately no production-at-risk figure.)"""
    p, vars_ = load_params(), load_varieties()
    out = []
    for b in load_blocks():
        a = con.execute("SELECT * FROM block_alerts WHERE block_id=? ORDER BY issued_on DESC LIMIT 1",
                        (b["block_id"],)).fetchone()
        subs = [dict(r) for r in con.execute("SELECT * FROM subscribers WHERE block_id=? AND active=1",
                                             (b["block_id"],))]
        susc = [s for s in subs if stage_mod.stage_for(s, date.today(), p, vars_)["susceptible"]]
        reports = con.execute(
            "SELECT COUNT(*) c FROM followups f JOIN subscribers s ON s.id=f.subscriber_id"
            " WHERE s.block_id=? AND f.answer='yes' AND f.officer_status='unverified'", (b["block_id"],)).fetchone()["c"]
        out.append({"block_id": b["block_id"], "block": b["block"], "level": a["level"] if a else None,
                    "issued_on": a["issued_on"] if a else None, "subscribers": len(subs),
                    "subscribers_in_susceptible_stage": len(susc), "unverified_yes_reports": reports})
    return out


@app.get("/officer/reports", dependencies=[Depends(officer_auth)])
def officer_reports(con=Depends(get_con)):
    return [dict(r) for r in con.execute(
        "SELECT f.id followup_id, s.block_id, s.variety, f.origin, f.answer, f.answered_at, f.officer_status"
        " FROM followups f JOIN subscribers s ON s.id=f.subscriber_id"
        " WHERE f.answer IN ('yes','unsure') AND f.officer_status='unverified' ORDER BY f.answered_at")]


@app.post("/officer/verify", dependencies=[Depends(officer_auth)])
def officer_verify(v: Verify, con=Depends(get_con)):
    cur = con.execute("UPDATE followups SET officer_status=?, officer_note=? WHERE id=?",
                      (v.status, v.note, v.followup_id))
    con.commit()
    if not cur.rowcount:
        raise HTTPException(404, "unknown followup")
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
