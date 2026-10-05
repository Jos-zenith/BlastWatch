from datetime import date, datetime, timedelta

from sqlalchemy import select

from blastwatch import config
from blastwatch.models import Alert, District, RiskDaily


def ids(client):
    return {d["name"]: d["id"] for d in client.get("/api/districts").json()}


def test_health_reports_forecast_freshness(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and body["districts"] == 17
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
    assert first["production_exposed_t"] == round(100_000 * 4313.2 / 1000)
    assert last["level"] == "Low" and last["production_exposed_t"] is None


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
                 "leaf_wet_min": 60 if 8 <= i < 21 else 0} for i in range(24)]
    res = client.post("/api/sensors/readings", headers={"X-API-Key": "secret"},
                      json={"station_id": "MDU-01", "district": "madurai", "readings": readings})
    assert res.status_code == 202 and res.json()["accepted"] == 24
    with seeded() as s:
        d = s.scalar(select(District).where(District.name == "Madurai"))
        today = s.scalar(select(RiskDaily).where(RiskDaily.district_id == d.id, RiskDaily.date == date.today(),
                                                 RiskDaily.model_version == "rules-v2"))
    assert today.wetness_basis == "sensor"
    assert today.longest_wet_run == 13 and today.level == "High"


def test_production_and_genes(client):
    prod = client.get("/api/production", params={"areas": ["India", "Nowhere"]}).json()
    assert prod["series"]["India"][0]["year"] == 2024
    assert prod["series"]["Nowhere"] == []
    genes = client.get("/api/genes").json()
    assert {"Pi54", "Pi9", "AVR-Pita"} <= {g["symbol"] for g in genes}
    assert client.get("/api/genes/pi54").json()["symbol"] == "Pi54"
    assert client.get("/api/genes/nope").status_code == 404


def test_pages_are_served(client):
    assert "officer.js" in client.get("/").text
    assert "research.js" in client.get("/research.html").text
