from datetime import date, datetime, timedelta

from sqlalchemy import select

from blastwatch import config
from blastwatch.models import Alert, District, RiskDaily
from blastwatch.risk import load_rules


def ids(client):
    return {d["name"]: d["id"] for d in client.get("/api/districts").json()}


def test_health_reports_forecast_freshness(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and body["districts"] == 18
    assert body["forecast"]["status"] == "fresh"
    assert "last_ok" in body["sources"]["open-meteo"]


def test_outlook_puts_alert_districts_first(client):
    body = client.get("/api/outlook").json()
    first = body["districts"][0]
    assert first["name"] == "Thanjavur" and first["action"] == "alert"
    assert first["days"][0]["horizon"] == "near"
    assert first["days"][0]["wetness_basis"] == "rh"  # synthetic data has no dew point / LWP
    madurai = next(d for d in body["districts"] if d["name"] == "Madurai")
    assert madurai["action"] == "none"
    # Districts without any weather are "unknown", not silently "none".
    assert next(d for d in body["districts"] if d["name"] == "Erode")["action"] == "unknown"


def test_risk_map_ranks_wet_district_first(client):
    body = client.get("/api/risk").json()
    assert body["date"] == date.today().isoformat()
    first, last = body["districts"][0], body["districts"][-1]
    assert first["name"] == "Thanjavur" and first["level"] == "High"
    assert last["level"] == "Low"


def test_district_endpoints(client):
    tid = ids(client)["Thanjavur"]
    assert client.get(f"/api/districts/{tid}/risk").json()["series"]
    assert client.get(f"/api/districts/{tid}/weather").json()["hours"]
    advice = client.get(f"/api/districts/{tid}/advice").json()
    assert advice["level"] == "High"
    assert "Spray only if lesions are found" in advice["message"]
    assert "CO 51" in [v["name"] for v in advice["varieties"]]
    assert "Pusa Basmati 1637" not in [v["name"] for v in advice["varieties"]]
    assert client.get("/api/districts/9999/risk").status_code == 404


def test_alerts_list_and_mark_sent(client, seeded):
    with seeded() as s:
        d = s.scalar(select(District).where(District.name == "Thanjavur"))
        s.add(Alert(district_id=d.id, episode_start=date.today(), high_days=date.today().isoformat(),
                    message_en="en", message_ta="ta", status="ready", created_at=datetime.now()))
        s.commit()
    [alert] = client.get("/api/alerts").json()
    assert alert["district_ta"] == "தஞ்சாவூர்" and alert["status"] == "ready"
    assert client.post(f"/api/alerts/{alert['id']}/sent").json()["status"] == "sent"
    assert client.post("/api/alerts/999/sent").status_code == 404


def test_sensor_ingest_requires_key(client, monkeypatch):
    body = {"station_id": "TNJ-01", "district": "Madurai",
            "readings": [{"ts": datetime.now().isoformat(), "temp_c": 25, "leaf_wet_min": 60}]}
    monkeypatch.setattr(config, "INGEST_KEY", "")
    assert client.post("/api/sensors/readings", json=body).status_code == 503
    monkeypatch.setattr(config, "INGEST_KEY", "secret")
    assert client.post("/api/sensors/readings", json=body, headers={"X-API-Key": "nope"}).status_code == 401
    bad = {**body, "station_id": "has spaces"}
    assert client.post("/api/sensors/readings", json=bad, headers={"X-API-Key": "secret"}).status_code == 422


def test_sensor_readings_override_grid_weather(client, seeded, monkeypatch):
    monkeypatch.setattr(config, "INGEST_KEY", "secret")
    # Grid weather says Madurai is dry; a canopy sensor reports long leaf wetness overnight.
    start = datetime.combine(date.today() - timedelta(days=1), datetime.min.time()).replace(hour=12)
    readings = [{"ts": (start + timedelta(hours=i)).isoformat(), "temp_c": 25.0,
                 "leaf_wet_min": 60 if 4 <= i < 21 else 0} for i in range(24)]
    res = client.post("/api/sensors/readings", headers={"X-API-Key": "secret"},
                      json={"station_id": "MDU-01", "district": "madurai", "readings": readings})
    assert res.status_code == 202 and res.json()["accepted"] == 24
    with seeded() as s:
        d = s.scalar(select(District).where(District.name == "Madurai"))
        today = s.scalar(select(RiskDaily).where(RiskDaily.district_id == d.id, RiskDaily.date == date.today(),
                                                 RiskDaily.model_version == load_rules()["model_version"]))
    assert today.wetness_basis == "sensor"
    assert today.longest_wet_run == 17 and today.level == "High"


def test_sensor_without_wetness_does_not_mask_grid_weather(client, seeded, monkeypatch):
    monkeypatch.setattr(config, "INGEST_KEY", "secret")
    # Grid weather says Thanjavur's night is wet; a sensor reporting only temperature must not
    # turn those hours dry and drop the day to Low.
    start = datetime.combine(date.today() - timedelta(days=1), datetime.min.time()).replace(hour=12)
    readings = [{"ts": (start + timedelta(hours=i)).isoformat(), "temp_c": 25.0} for i in range(24)]
    res = client.post("/api/sensors/readings", headers={"X-API-Key": "secret"},
                      json={"station_id": "TNJ-01", "district": "thanjavur", "readings": readings})
    assert res.status_code == 202
    with seeded() as s:
        d = s.scalar(select(District).where(District.name == "Thanjavur"))
        today = s.scalar(select(RiskDaily).where(RiskDaily.district_id == d.id, RiskDaily.date == date.today(),
                                                 RiskDaily.model_version == load_rules()["model_version"]))
    assert today.wetness_basis == "rh" and today.level == "High"


def test_production_and_genes(client):
    prod = client.get("/api/production", params={"areas": ["India", "Nowhere"]}).json()
    assert prod["series"]["India"][0]["year"] == 2024
    assert prod["series"]["Nowhere"] == []
    genes = client.get("/api/genes").json()
    assert {"Pi54", "Pi9", "AVR-Pita"} <= {g["symbol"] for g in genes}
    assert client.get("/api/genes/pi54").json()["symbol"] == "Pi54"
    assert client.get("/api/genes/nope").status_code == 404


def test_pages_are_served(client):
    """The built Vue app: every page path returns the app shell, bundles are served, API paths are not."""
    import re

    if not (config.WEB_DIR / "index.html").exists():
        import pytest
        pytest.skip("frontend not built (cd frontend && npm run build)")
    shell = client.get("/").text
    assert '<div id="app">' in shell
    for path in ("/live", "/confidence", "/research", "/research.html"):
        assert client.get(path).text == shell
    bundle = re.search(r'src="(/assets/[^"]+\.js)"', shell).group(1)
    assert client.get(bundle).status_code == 200
    assert client.get("/api/no-such-endpoint").status_code == 404
    assert client.get("/../blastwatch/config.py").text == shell  # no path traversal out of the build


def test_field_check_becomes_an_observation(client, seeded):
    from blastwatch import alerts
    with seeded() as s:
        alerts.evaluate(s, load_rules())
    tid, mid = ids(client)["Thanjavur"], ids(client)["Madurai"]
    [alert] = client.get("/api/alerts").json()
    body = {"blast_found": True, "fields_checked": 5, "note": "leaf blast in 2 fields", "alert_id": alert["id"],
            "block": "Budalur", "variety": "ADT 43", "crop_stage": "booting", "severity_ses": 5}
    res = client.post(f"/api/districts/{tid}/field-checks", json=body)
    assert res.status_code == 201
    # A routine visit with no alert counts; another district's alert does not.
    assert client.post(f"/api/districts/{mid}/field-checks", json={"blast_found": False}).status_code == 201
    bad = {"blast_found": False, "alert_id": alert["id"]}
    assert client.post(f"/api/districts/{mid}/field-checks", json=bad).status_code == 404
    assert client.get("/api/alerts").json()[0]["field_checks"][0]["blast_found"] is True
    with seeded() as s:
        from blastwatch.models import Observation
        obs = {o.source: o for o in s.scalars(select(Observation))}
    found = obs[res.json()["observation"]]
    assert found.blast_present is True
    assert (found.block, found.variety, found.crop_stage, found.severity) == ("Budalur", "ADT 43", "booting", "SES 5")
    assert len(obs) == 2
    bad_ses = {"blast_found": True, "severity_ses": 10}
    assert client.post(f"/api/districts/{tid}/field-checks", json=bad_ses).status_code == 422
    counts = client.get("/api/validation").json()["observations"]
    assert counts["complete"] == 1 and counts["with_variety"] == 1
    checks = client.get(f"/api/districts/{tid}/field-checks").json()
    assert checks[0]["blast_found"] is True and checks[0]["fields_checked"] == 5
    outlook = {d["name"]: d for d in client.get("/api/outlook").json()["districts"]}
    assert outlook["Thanjavur"]["last_check"]["blast_found"] is True


def test_outlook_explains_each_score(client):
    day = client.get("/api/outlook").json()["districts"][0]["days"][0]
    assert set(day["points"]) == {"run", "hours", "rain", "cloud"}
    assert round(sum(day["points"].values()) * day["susceptibility"], 1) == day["score"]


def test_advice_offers_no_breeding_lines(client):
    advice = client.get(f"/api/districts/{ids(client)['Thanjavur']}/advice").json()
    assert all(v["status"] == "released" for v in advice["varieties"])
    assert advice["next_season"] is None
