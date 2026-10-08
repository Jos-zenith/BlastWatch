from datetime import date, datetime, timedelta

from sqlalchemy import delete, select

from blastwatch import alerts
from blastwatch.calibration import contingency, import_observations
from blastwatch.models import Alert, IngestRun, Observation
from blastwatch.risk import load_rules

RULES = load_rules()


def test_alert_raised_once_for_sustained_risk(seeded):
    with seeded() as s:
        result = alerts.evaluate(s, RULES)
        assert result == {"created": ["Thanjavur"], "updated": [], "expired": [], "suppressed": None}
        [alert] = s.scalars(select(Alert)).all()
        assert "தஞ்சாவூர்" in alert.message_ta and "Thanjavur" in alert.message_en
        assert "Spray only if spots or neck rot are found" in alert.message_en
        assert "panicle neck" in alert.message_en and "கதிர்க் கழுத்து" in alert.message_ta
        assert alert.status == "ready"
        # Same episode on the next run: no duplicate.
        assert alerts.evaluate(s, RULES)["created"] == []


def test_alerts_suppressed_when_forecast_expired(seeded):
    with seeded() as s:
        s.execute(delete(IngestRun))
        old = datetime.now() - timedelta(hours=48)
        s.add(IngestRun(source="open-meteo", started_at=old, finished_at=old, status="ok", rows=1))
        s.commit()
        result = alerts.evaluate(s, RULES)
        assert result["created"] == [] and "expired" in result["suppressed"]


def test_single_high_day_does_not_alert(seeded):
    with seeded() as s:
        # Only look at a 1-day horizon: one High day is below min_high_days (2).
        rules = {**RULES, "alerts": {**RULES["alerts"], "horizon_days": 1}}
        assert alerts.evaluate(s, rules)["created"] == []


def test_contingency_counts():
    d = date(2025, 11, 10)
    scores = {(1, d - timedelta(days=2)): 80.0, (1, d): 20.0, (2, d): 20.0, (3, d): 70.0}
    obs = [(1, d, True),   # warned 2 days ahead -> TP
           (2, d, True),   # never warned -> FN
           (3, d, False),  # warned, no blast -> FP
           (2, d, False),  # quiet, no blast -> TN
           (9, d, True)]   # no weather -> skipped
    scores_65, skipped = contingency(obs, scores, 65, lead_days=3)
    assert (scores_65.tp, scores_65.fp, scores_65.fn, scores_65.tn, skipped) == (1, 1, 1, 1, 1)
    assert scores_65.pod == 0.5 and scores_65.far == 0.5 and scores_65.csi == 1 / 3
    strict, _ = contingency(obs, scores, 90, lead_days=3)
    assert (strict.tp, strict.fp) == (0, 0) and strict.far is None


def test_import_observations(Session, tmp_path):
    path = tmp_path / "obs.csv"
    path.write_text("district,state,date,blast_present,severity,source,note\n"
                    "Thanjavur,Tamil Nadu,2023-11-20,1,leaf blast moderate,KVK survey,\n"
                    "Madurai,Tamil Nadu,2023-11-20,no,,KVK survey,checked 5 fields\n", encoding="utf-8")
    with Session() as s:
        assert import_observations(s, path) == 2
        assert [o.blast_present for o in s.scalars(select(Observation).order_by(Observation.id))] == [True, False]
    bad = tmp_path / "bad.csv"
    bad.write_text("district,state,date,blast_present,severity,source,note\n"
                   "Atlantis,Tamil Nadu,2023-11-20,1,,x,\n", encoding="utf-8")
    with Session() as s:
        try:
            import_observations(s, bad)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "Atlantis" in str(e)


def test_merged_hours_can_be_pinned_to_one_source(seeded):
    from blastwatch.db import upsert
    from blastwatch.models import District, WeatherHourly
    from blastwatch.pipeline import merged_hours

    with seeded() as s:
        tid = s.scalar(select(District.id).where(District.name == "Thanjavur"))
        ts = datetime.combine(date.today(), datetime.min.time()).replace(hour=3)
        upsert(s, WeatherHourly, [{"district_id": tid, "ts": ts, "source": "archive-forecast:d3",
                                   "temp_c": 25.0, "rh_pct": 50.0, "is_forecast": True,
                                   "fetched_at": datetime.now()}], ["district_id", "ts", "source"])
        merged = {h.ts: h for h in merged_hours(s, tid)}
        assert merged[ts].rh_pct == 97  # the live forecast outranks the archive
        pinned = merged_hours(s, tid, "archive-forecast")
        assert [(h.ts, h.rh_pct) for h in pinned] == [(ts, 50.0)]


def test_message_dates_use_weekdays_in_each_language():
    days = [date(2026, 10, 7), date(2026, 10, 8)]
    assert alerts.format_dates(days, "en") == "Wed 7 Oct, Thu 8 Oct"
    assert alerts.format_dates(days, "ta") == "புதன் 7/10, வியாழன் 8/10"


def test_ready_message_follows_the_forecast_then_expires(seeded):
    with seeded() as s:
        alerts.evaluate(s, RULES)
        alert = s.scalars(select(Alert)).one()
        # The forecast moves on: pretend the message was written for days that have now passed.
        old = date.today() - timedelta(days=3)
        alert.high_days = f"{old};{old + timedelta(days=1)}"
        s.commit()
        result = alerts.evaluate(s, RULES)
        assert result["updated"] == ["Thanjavur"]
        s.refresh(alert)
        assert alert.high_days.split(";")[0] >= date.today().isoformat() and alert.updated_at is not None
        # Days later, nothing is High any more: the unsent message expires instead of going out stale.
        result = alerts.evaluate(s, RULES, today=date.today() + timedelta(days=10))
        s.refresh(alert)
        assert alert.id in result["expired"] and alert.status == "expired"


def test_sent_message_is_never_rewritten(seeded):
    with seeded() as s:
        alerts.evaluate(s, RULES)
        alert = s.scalars(select(Alert)).one()
        alert.status, alert.high_days = "sent", "2000-01-01;2000-01-02"
        s.commit()
        assert alerts.evaluate(s, RULES)["updated"] == []
        s.refresh(alert)
        assert alert.status == "sent" and alert.high_days == "2000-01-01;2000-01-02"


def test_message_is_withdrawn_when_risk_eases_and_reissued_when_it_returns(seeded):
    with seeded() as s:
        alerts.evaluate(s, RULES)
        alert = s.scalars(select(Alert)).one()
        # Stricter rules (or a drier forecast): Thanjavur no longer meets the alert rule.
        strict = {**RULES, "alerts": {**RULES["alerts"], "min_high_days": 99}}
        assert alerts.evaluate(s, strict)["expired"] == [alert.id]
        s.refresh(alert)
        assert alert.status == "expired"
        # The risk is back within the cooldown: the same episode's message is reissued, not duplicated.
        assert alerts.evaluate(s, RULES)["updated"] == ["Thanjavur"]
        s.refresh(alert)
        assert alert.status == "ready" and s.scalars(select(Alert)).one().id == alert.id
