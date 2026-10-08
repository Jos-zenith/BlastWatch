import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from blastwatch import risk, stage  # noqa: E402
from blastwatch.config import load_params  # noqa: E402

P = load_params()
TODAY = date(2025, 11, 10)


def fake_rows(start: date, days: int, humid_days: set[int], hours_humid=14) -> list[dict]:
    rows = []
    for d in range(days):
        for h in range(24):
            humid = d in humid_days and h < hours_humid
            rows.append({"ts": f"{(start + timedelta(d)).isoformat()}T{h:02d}:00",
                         "temp": 25.0, "rh": 95.0 if humid else 60.0, "precip": 0.0, "wind": 0.5})
    return rows


def test_favorable_hour_bounds():
    base = {"temp": 25, "rh": 95, "precip": 0, "wind": 0.5}
    assert risk.is_favorable(base, P)
    assert not risk.is_favorable({**base, "temp": 35}, P)
    assert not risk.is_favorable({**base, "rh": 70}, P)
    assert risk.is_favorable({**base, "rh": 70, "precip": 1.0}, P)       # rain counts as wet
    assert not risk.is_favorable({**base, "temp": None}, P)              # missing data never favorable


def test_levels():
    high = risk.block_level(risk.daily_favorable_hours(fake_rows(TODAY, 7, {0, 1}), P), TODAY, P)
    watch = risk.block_level(risk.daily_favorable_hours(fake_rows(TODAY, 7, {1}), P), TODAY, P)
    low = risk.block_level(risk.daily_favorable_hours(fake_rows(TODAY, 7, set()), P), TODAY, P)
    short = risk.block_level(risk.daily_favorable_hours(fake_rows(TODAY, 2, {0, 1}), P), TODAY, P)
    assert (high["level"], watch["level"], low["level"]) == ("HIGH", "WATCH", "LOW")
    assert short["level"] == "NO_DATA"                                    # incomplete forecast never alerts


def test_stage_branches():
    v = {"X": {"duration_days": 100, "nursery_age_days": 20, "blast_rating": "unknown", "source": "t"},
         "Other/Unknown": {"duration_days": 130, "nursery_age_days": None, "blast_rating": "unknown", "source": "default"}}
    est = (TODAY - timedelta(days=30)).isoformat()
    tp = stage.stage_for({"variety": "X", "method": "transplanted", "establish_date": est}, TODAY, P, v)
    ds = stage.stage_for({"variety": "X", "method": "direct_seeded", "establish_date": est}, TODAY, P, v)
    assert tp["age_days"] == 50 and ds["age_days"] == 30                  # nursery offset only when transplanted
    assert tp["stage"] == "booting" and ds["stage"] == "vegetative"
    unk = stage.stage_for({"variety": "Nope", "method": "transplanted", "establish_date": est}, TODAY, P, v)
    assert "variety_unknown" in unk["flags"] and "nursery_age_default" in unk["flags"]
    late = stage.stage_for({"variety": "X", "method": "direct_seeded",
                            "establish_date": (TODAY - timedelta(days=200)).isoformat()}, TODAY, P, v)
    assert late["stage"] == "outside_season" and not late["susceptible"]


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("BLASTWATCH_DB", str(tmp_path / "t.db"))
    from blastwatch.db import connect
    return connect()


def _enrol(con, contact, block, est_days_ago, method="direct_seeded", variety="White Ponni"):
    con.execute("INSERT INTO subscribers(contact,block_id,variety,method,establish_date,consent_at,"
                "consent_text_version) VALUES(?,?,?,?,?,?,?)",
                (contact, block, variety, method, (TODAY - timedelta(days=est_days_ago)).isoformat(), "now", "t"))
    con.commit()


def test_pipeline_alert_cooldown_followups_and_stage_gate(con):
    from blastwatch.pipeline import run_daily
    _enrol(con, "a", "TNJ-BUD", 70)      # direct-seeded 70d of 137 => booting => alerted
    _enrol(con, "b", "TNJ-BUD", 120)     # ripening => not alerted
    humid = lambda lat, lon, days: fake_rows(TODAY, 7, {0, 1})
    s = run_daily(con, TODAY, fetch=humid)
    assert s["high"] == 6 and s["alerts_sent"] == 1
    msgs = con.execute("SELECT body FROM messages WHERE kind='alert'").fetchall()
    assert len(msgs) == 1 and "withheld" in msgs[0]["body"]              # unreviewed chemicals never sent
    assert con.execute("SELECT COUNT(*) c FROM followups WHERE origin='alert'").fetchone()["c"] == 2
    s2 = run_daily(con, TODAY + timedelta(days=1), fetch=lambda *a: fake_rows(TODAY + timedelta(1), 7, {0, 1}))
    assert s2["alerts_sent"] == 0                                         # cooldown
    run_daily(con, TODAY + timedelta(days=7), fetch=lambda *a: fake_rows(TODAY + timedelta(7), 7, set()))
    assert con.execute("SELECT COUNT(*) c FROM followups WHERE asked_at IS NOT NULL").fetchone()["c"] == 1


def test_fetch_failure_isolated(con):
    from blastwatch.pipeline import run_daily

    def boom(*a):
        raise RuntimeError("down")
    s = run_daily(con, TODAY, fetch=boom)
    assert s["blocks"] == 0 and len(s["errors"]) == 6                     # no crash, errors surfaced


def test_api_consent_reply_and_officer_gate(con, monkeypatch):
    from fastapi.testclient import TestClient
    from blastwatch.api import app
    monkeypatch.setenv("BLASTWATCH_OFFICER_KEY", "k")
    c = TestClient(app)
    body = {"contact": "9", "block_id": "TNJ-BUD", "variety": "CO 51", "method": "transplanted",
            "establish_date": (date.today() - timedelta(days=30)).isoformat(), "consent": False}
    assert c.post("/enrol", json=body).status_code == 400                 # no consent, no enrolment
    assert c.post("/enrol", json={**body, "consent": True}).status_code == 200
    assert c.post("/enrol", json={**body, "consent": True}).status_code == 409
    assert c.post("/enrol", json={**body, "contact": "8", "consent": True, "block_id": "X"}).status_code == 400
    sid = con.execute("SELECT id FROM subscribers WHERE contact='9'").fetchone()["id"]
    con.execute("INSERT INTO followups(subscriber_id,due_on,origin,asked_at) VALUES(?,?,?,?)",
                (sid, "2025-01-01", "alert", "x"))
    con.commit()
    assert c.post("/reply", json={"contact": "9", "answer": "yes"}).status_code == 200
    assert c.get("/officer/reports").status_code == 401
    r = c.get("/officer/reports", headers={"x-officer-key": "k"}).json()
    assert len(r) == 1 and r[0]["officer_status"] == "unverified"         # a yes is not a confirmation
    assert c.get("/status").json()[0]["unverified_yes_reports"] == 1


def test_eval_metrics_and_verdict_gates():
    import evaluate as ev
    assert ev.average_precision([3, 2, 1], [1, 0, 0]) == 1.0
    assert ev.average_precision([1, 2, 3], [1, 0, 0]) == pytest.approx(1 / 3)
    crit = __import__("yaml").safe_load((ROOT / "eval/criteria.yaml").read_text())
    table = [{"block": "b", "day": date(2020, 1, i + 1), "model": i, "base": 0, "label": int(i > 7),
              "model_alert": int(i > 7), "base_alert": 0} for i in range(10)]
    v = ev.verdict(table, n_events=2, n_blocks=1, crit=crit, ci=(0.1, 0.5))
    assert v["verdict"].startswith("INCONCLUSIVE")                        # a handful of events can never PASS
    v = ev.verdict(table, n_events=40, n_blocks=5, crit=crit, ci=(0.1, 0.5))
    assert v["verdict"].startswith("PASS")


def test_build_table_labels_and_baseline():
    import evaluate as ev
    crit = __import__("yaml").safe_load((ROOT / "eval/criteria.yaml").read_text())
    start = date(2020, 11, 1)
    rows = fake_rows(start, 40, set(range(0, 40)))
    t = ev.build_table({"b": rows}, {"b": [date(2020, 11, 10)]}, P, crit, start, start + timedelta(39))
    pos = [r["day"] for r in t if r["label"]]
    assert min(pos) == start and max(pos) == date(2020, 11, 7)             # onset Nov 10 minus 3 days
    assert all(r["base"] > 0 for r in t)                                   # inside Oct20-Dec15 window
