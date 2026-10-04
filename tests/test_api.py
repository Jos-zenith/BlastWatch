from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from blastwatch.api import create_app
from blastwatch.db import init_db, make_engine, upsert
from blastwatch.models import District, Production, WeatherHourly
from blastwatch.pipeline import compute_risk, load_seeds


@pytest.fixture
def client(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    init_db(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        load_seeds(s)
        thanjavur = s.query(District).filter_by(name="Thanjavur").one()
        madurai = s.query(District).filter_by(name="Madurai").one()
        thanjavur.rice_area_ha = 100_000

        # Three days of synthetic weather around today: Thanjavur wet nights, Madurai dry.
        start = datetime.combine(date.today() - timedelta(days=1), datetime.min.time()).replace(hour=12)
        rows = []
        for i in range(72):
            ts = start + timedelta(hours=i)
            night = ts.hour >= 20 or ts.hour < 9
            for d, rh in ((thanjavur, 97 if night else 70), (madurai, 55)):
                rows.append({"district_id": d.id, "ts": ts, "temp_c": 25.0, "rh_pct": rh,
                             "precip_mm": 0.2, "cloud_pct": 90, "source": "open-meteo",
                             "is_forecast": ts > datetime.now(), "fetched_at": datetime.now()})
        upsert(s, WeatherHourly, rows, ["district_id", "ts", "source"])
        upsert(s, Production, [
            {"area_code": 100, "area": "India", "item_code": 27, "item": "Rice", "year": 2024,
             "area_ha": 5.05e7, "production_t": 2.18e8, "yield_kg_ha": 4313.2, "source": "faostat"},
        ], ["area_code", "item_code", "year", "source"])
        s.commit()
        compute_risk(s)
    yield TestClient(create_app(session_factory=Session))


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and body["districts"] == 17


def test_risk_map_ranks_wet_district_first(client):
    body = client.get("/api/risk").json()
    assert body["date"] == date.today().isoformat()
    first, last = body["districts"][0], body["districts"][-1]
    assert first["name"] == "Thanjavur" and first["level"] == "High"
    assert first["production_exposed_t"] == round(100_000 * 4313.2 / 1000)
    assert last["level"] == "Low" and last["production_exposed_t"] is None


def test_district_endpoints(client):
    districts = {d["name"]: d["id"] for d in client.get("/api/districts").json()}
    tid = districts["Thanjavur"]
    assert client.get(f"/api/districts/{tid}/risk").json()["series"]
    assert client.get(f"/api/districts/{tid}/weather").json()["hours"]
    advice = client.get(f"/api/districts/{tid}/advice").json()
    assert advice["level"] == "High"
    assert "CO 51" in [v["name"] for v in advice["varieties"]]
    assert "Pusa Basmati 1637" not in [v["name"] for v in advice["varieties"]]
    assert client.get("/api/districts/9999/risk").status_code == 404


def test_production_and_genes(client):
    prod = client.get("/api/production", params={"areas": ["India", "Nowhere"]}).json()
    assert prod["series"]["India"][0]["year"] == 2024
    assert prod["series"]["Nowhere"] == []
    genes = client.get("/api/genes").json()
    assert {"Pi54", "Pi9", "AVR-Pita"} <= {g["symbol"] for g in genes}
    assert client.get("/api/genes/pi54").json()["symbol"] == "Pi54"
    assert client.get("/api/genes/nope").status_code == 404


def test_dashboard_is_served(client):
    res = client.get("/")
    assert res.status_code == 200 and "BlastWatch" in res.text
