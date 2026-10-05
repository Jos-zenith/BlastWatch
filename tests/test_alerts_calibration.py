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
        assert result == {"created": ["Thanjavur"], "suppressed": None}
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
