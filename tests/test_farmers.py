"""Crop stage, farmer alerts and check-ins, officer verification, farmer API, evaluation."""
import re
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from blastwatch import calibration, config, farmers
from blastwatch.models import District, FarmerMessage, Followup, Observation, Subscriber
from blastwatch.risk import load_rules
from blastwatch.stage import CropVariety, crop_stage, load_crop_calendar

RULES = load_rules()
CAL = {"CO 51": CropVariety("CO 51", 107, None, "test"), "ADT 38": CropVariety("ADT 38", 132, 25, "test")}
TODAY = date.today()
MORNING = datetime.combine(TODAY, time(10))


def days_ago(n: int) -> date:
    return TODAY - timedelta(days=n)


@pytest.mark.parametrize("age, stage", [(10, "vegetative"), (42, "booting"), (67, "heading"),
                                        (77, "ripening"), (122, "ripening"), (123, "harvested")])
def test_stage_counts_back_from_maturity(age, stage):
    # CO 51, 107 days: heading-flowering is days 67-76, ripening 77 to maturity + 15.
    assert crop_stage("CO 51", "direct_seeded", days_ago(age), TODAY, RULES, CAL).name == stage


def test_transplanted_age_adds_nursery_and_flags_the_default():
    known = crop_stage("ADT 38", "transplanted", days_ago(10), TODAY, RULES, CAL)
    assert known.age_days == 35 and known.flags == ()
    default = crop_stage("CO 51", "transplanted", days_ago(10), TODAY, RULES, CAL)
    assert default.age_days == 31 and default.flags == ("nursery_age_default",)


def test_unknown_variety_is_still_alerted_until_any_crop_would_be_harvested():
    st = crop_stage("Local red rice", "direct_seeded", days_ago(60), TODAY, RULES, CAL)
    assert st.name == "unknown" and st.susceptible and "variety_unknown" in st.flags
    assert crop_stage("Local red rice", "direct_seeded", days_ago(160), TODAY, RULES, CAL).name == "harvested"


def test_seed_crop_calendar_loads():
    assert load_crop_calendar()["CO 51"].duration_days == 107


def test_messages_are_complete_in_one_language_and_follow_the_stage():
    d = District(name="Thanjavur", name_ta="தஞ்சாவூர்", state="Tamil Nadu", lat=0, lon=0)
    days = [TODAY, TODAY + timedelta(days=1)]
    ta = farmers.compose_alert(d, "heading", days, "ta")
    assert "தஞ்சாவூர்" in ta and not re.search(r"[A-Za-z]", ta)
    assert "கதிர்" in ta and "யூரியா" not in ta
    en_neck = farmers.compose_alert(d, "heading", days, "en")
    en_leaf = farmers.compose_alert(d, "vegetative", days, "en")
    assert "panicle" in en_neck and "urea" not in en_neck
    assert "eye-shaped spots" in en_leaf and "Spray only if spots are found" in en_leaf
    for text in (ta, en_neck, en_leaf):
        assert "withheld" not in text and "[" not in text
    assert not re.search(r"[A-Za-z]", farmers.CHECKIN_TEXT["ta"].replace("BlastWatch", ""))


def enrol(s, contact, district, variety, age, method="direct_seeded", language="en", channel="outbox"):
    d = s.scalar(select(District).where(District.name == district))
    sub = Subscriber(contact=contact, channel=channel, district_id=d.id, variety=variety, method=method,
                     establish_date=days_ago(age), language=language, consent_at=datetime.now(),
                     consent_version="test", created_at=datetime.now())
    s.add(sub)
    s.commit()
    return sub


def test_alerts_target_susceptible_stages_in_alerting_districts(seeded):
    with seeded() as s:
        heading = enrol(s, "+91-1", "Thanjavur", "CO 51", 70, language="ta")
        ripening = enrol(s, "+91-2", "Thanjavur", "CO 51", 90)
        unknown = enrol(s, "+91-3", "Thanjavur", "Local variety", 40)
        enrol(s, "+91-4", "Madurai", "CO 51", 20)  # dry district: no alert
        result = farmers.notify_farmers(s, RULES, now=MORNING)
        assert result["alerts"] == 2 and result["skipped"] is None
        msgs = {m.subscriber_id: m for m in s.scalars(select(FarmerMessage))}
        assert set(msgs) == {heading.id, unknown.id}
        assert msgs[heading.id].stage == "heading" and "கதிர்" in msgs[heading.id].body
        assert msgs[heading.id].status == "queued"
        assert ripening.id not in msgs
        due = sorted(f.due_on for f in s.scalars(select(Followup).where(Followup.subscriber_id == heading.id)))
        assert due == [TODAY + timedelta(days=7), TODAY + timedelta(days=14)]
        # The next refresh is inside the cooldown: no repeat.
        assert farmers.notify_farmers(s, RULES, now=MORNING + timedelta(hours=3))["alerts"] == 0


def test_no_farmer_messages_outside_send_hours(seeded):
    with seeded() as s:
        enrol(s, "+91-1", "Thanjavur", "CO 51", 70)
        night = datetime.combine(TODAY, time(2))
        assert farmers.notify_farmers(s, RULES, now=night)["skipped"] == "outside send hours"
        assert s.scalar(select(FarmerMessage.id)) is None


def test_failed_delivery_does_not_start_the_cooldown(seeded):
    with seeded() as s:
        sub = enrol(s, "chat-1", "Thanjavur", "CO 51", 20, channel="telegram")
        assert farmers.notify_farmers(s, RULES, now=MORNING, send=lambda *a: "failed:no_token")["alerts"] == 0
        assert s.scalar(select(Followup.id)) is None
        assert farmers.notify_farmers(s, RULES, now=MORNING + timedelta(hours=3),
                                      send=lambda *a: "sent")["alerts"] == 1
        statuses = [m.status for m in s.scalars(select(FarmerMessage).where(FarmerMessage.subscriber_id == sub.id))]
        assert statuses == ["failed:no_token", "sent"]


def test_checkin_reply_and_verification_become_an_observation(seeded):
    with seeded() as s:
        sub = enrol(s, "+91-1", "Thanjavur", "CO 51", 20, language="ta")
        farmers.notify_farmers(s, RULES, now=MORNING)
        week_later = MORNING + timedelta(days=7)
        assert farmers.send_due_checkins(s, RULES, week_later) == 1
        checkin = s.scalars(select(FarmerMessage).where(FarmerMessage.kind == "checkin")).one()
        assert checkin.body == farmers.CHECKIN_TEXT["ta"]
        # A second due question waits while the first is open.
        assert farmers.send_due_checkins(s, RULES, week_later + timedelta(days=7)) == 0

        followup = farmers.record_reply(s, "+91-1", "yes", now=week_later + timedelta(hours=5))
        assert followup.officer_status == "unverified"
        assert farmers.record_reply(s, "+91-unknown", "yes") is None
        assert s.scalar(select(Observation.id)) is None  # a farmer's YES alone is not an observation

        farmers.verify_report(s, followup.id, "confirmed", "leaf blast, 3 hills")
        obs = s.scalars(select(Observation)).one()
        assert obs.blast_present and obs.source == f"farmer-report:{followup.id}"
        assert obs.district_id == sub.district_id and obs.date == followup.answered_at.date()
        farmers.verify_report(s, followup.id, "rejected", "brown spot")  # officer revises the verdict
        assert not s.scalars(select(Observation)).one().blast_present
        with pytest.raises(LookupError):
            farmers.verify_report(s, 999, "confirmed")


def test_unanswered_question_stops_blocking_after_answer_window(seeded):
    with seeded() as s:
        enrol(s, "+91-1", "Thanjavur", "CO 51", 20)
        farmers.notify_farmers(s, RULES, now=MORNING)
        assert farmers.send_due_checkins(s, RULES, MORNING + timedelta(days=7)) == 1
        assert farmers.send_due_checkins(s, RULES, MORNING + timedelta(days=14, hours=1)) == 1


OFFICER = {"X-Officer-Key": "officer-secret"}


def test_farmer_endpoints_require_the_officer_key(client, monkeypatch):
    monkeypatch.setattr(config, "OFFICER_KEY", "")
    assert client.get("/api/subscribers").status_code == 503
    monkeypatch.setattr(config, "OFFICER_KEY", "officer-secret")
    assert client.get("/api/subscribers", headers={"X-Officer-Key": "nope"}).status_code == 401
    assert client.post("/api/replies", json={"contact": "x", "answer": "yes"}).status_code == 401
    assert client.get("/api/consent").json()["text"]["ta"]  # consent text is public


def test_enrolment_api_and_outlook_counts(client, monkeypatch):
    monkeypatch.setattr(config, "OFFICER_KEY", "officer-secret")
    body = {"contact": "+91-99", "district": "thanjavur", "variety": "CO 51", "method": "transplanted",
            "establish_date": days_ago(10).isoformat(), "consent": False}
    assert client.post("/api/subscribers", json=body, headers=OFFICER).status_code == 400
    body["consent"] = True
    first = client.post("/api/subscribers", json=body, headers=OFFICER).json()
    assert first["created"] and first["stage"] == "vegetative" and first["flags"] == ["nursery_age_default"]
    future = {**body, "establish_date": (TODAY + timedelta(days=1)).isoformat()}
    assert client.post("/api/subscribers", json=future, headers=OFFICER).status_code == 400
    assert client.post("/api/subscribers", json={**body, "district": "Atlantis"}, headers=OFFICER).status_code == 404
    # Re-enrolling the same contact updates the crop for the new season.
    again = client.post("/api/subscribers", json={**body, "variety": "ADT 38"}, headers=OFFICER).json()
    assert again["id"] == first["id"] and not again["created"]

    thanjavur = next(d for d in client.get("/api/outlook").json()["districts"] if d["name"] == "Thanjavur")
    assert thanjavur["farmers"] == {"subscribers": 1, "susceptible": 1, "unverified_reports": 0}
    assert client.post("/api/subscribers/stop", json={"contact": "+91-99"}, headers=OFFICER).json()["active"] is False


def test_outbox_reply_and_verify_api(client, seeded, monkeypatch):
    monkeypatch.setattr(config, "OFFICER_KEY", "officer-secret")
    with seeded() as s:
        enrol(s, "+91-1", "Thanjavur", "CO 51", 20)
        farmers.notify_farmers(s, RULES, now=MORNING)
        farmers.send_due_checkins(s, RULES, MORNING + timedelta(days=7))
    outbox = client.get("/api/farmer-messages", headers=OFFICER).json()
    assert [m["kind"] for m in outbox] == ["alert", "checkin"]
    assert client.post(f"/api/farmer-messages/{outbox[0]['id']}/sent", headers=OFFICER).json()["status"] == "sent"
    assert len(client.get("/api/farmer-messages", headers=OFFICER).json()) == 1

    assert client.post("/api/replies", json={"contact": "+91-1", "answer": "yes"}, headers=OFFICER).status_code == 200
    [report] = client.get("/api/reports", headers=OFFICER).json()
    assert report["district"] == "Thanjavur" and report["answer"] == "yes"
    thanjavur = next(d for d in client.get("/api/outlook").json()["districts"] if d["name"] == "Thanjavur")
    assert thanjavur["farmers"]["unverified_reports"] == 1
    done = client.post(f"/api/reports/{report['id']}/verify", json={"status": "confirmed"}, headers=OFFICER)
    assert done.json()["officer_status"] == "confirmed"
    assert client.get("/api/reports", headers=OFFICER).json() == []
    assert client.post("/api/reports/999/verify", json={"status": "confirmed"}, headers=OFFICER).status_code == 404


def test_average_precision_and_season_window():
    assert calibration.average_precision([0.9, 0.8, 0.1], [1, 1, 0]) == 1.0
    assert calibration.average_precision([0.1, 0.9], [1, 0]) == 0.5
    assert calibration.average_precision([0.5], [0]) is None
    wrap = {"season_start_mmdd": "11-01", "season_end_mmdd": "02-28"}
    assert calibration.in_season(date(2025, 1, 15), wrap) and not calibration.in_season(date(2025, 6, 1), wrap)


def _rows(n_present, n_absent, model_good=True):
    rows = []
    for i in range(n_present + n_absent):
        present = i < n_present
        good = 80.0 if present else 20.0
        rows.append({"district_id": i % 4, "date": date(2024, 1 + i % 12, 1), "label": int(present),
                     "model": good if model_good else 50.0, "baseline": 50.0,
                     "model_alert": int(present) if model_good else 0, "baseline_alert": 1})
    return rows


def test_verdict_is_inconclusive_without_enough_observations():
    v = calibration.verdict(_rows(10, 40), calibration.load_criteria())
    assert v["verdict"].startswith("INCONCLUSIVE") and v["ap_gain_ci95"] is None


def test_verdict_passes_only_when_the_model_beats_the_baseline():
    crit = calibration.load_criteria()
    crit["pass"]["bootstrap_iterations"] = 200
    assert calibration.verdict(_rows(40, 40), crit)["verdict"].startswith("PASS")
    assert calibration.verdict(_rows(40, 40, model_good=False), crit)["verdict"].startswith("FAIL")


def test_evaluation_table_matches_observations_to_lagged_weather(seeded):
    with seeded() as s:
        thanjavur = s.scalar(select(District).where(District.name == "Thanjavur"))
        s.add_all([
            Observation(district_id=thanjavur.id, date=TODAY + timedelta(days=5), blast_present=True, source="t"),
            Observation(district_id=thanjavur.id, date=TODAY - timedelta(days=60), blast_present=False, source="t"),
        ])
        s.commit()
        rows = calibration.evaluation_table(s, RULES, calibration.load_criteria(), TODAY - timedelta(days=90),
                                            TODAY + timedelta(days=30))
    assert len(rows) == 1  # the old observation has no weather in its lead window
    assert rows[0]["label"] == 1 and rows[0]["model_alert"] == 1
