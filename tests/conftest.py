from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from blastwatch.api import create_app
from blastwatch.db import init_db, make_engine, upsert
from blastwatch.models import District, IngestRun, Production, WeatherHourly
from blastwatch.pipeline import compute_risk, load_seeds


def synthetic_weather(district_id: int, wet_nights: bool, days: int = 4) -> list[dict]:
    """Hourly rows from yesterday noon: saturated nights (20:00-08:59) or dry throughout."""
    start = datetime.combine(date.today() - timedelta(days=1), datetime.min.time()).replace(hour=12)
    rows = []
    for i in range(24 * days):
        ts = start + timedelta(hours=i)
        night = ts.hour >= 20 or ts.hour < 9
        rows.append({"district_id": district_id, "ts": ts, "temp_c": 25.0,
                     "rh_pct": 97 if wet_nights and night else 60, "precip_mm": 0.2, "cloud_pct": 90,
                     "dew_point_c": None, "leaf_wet_prob": None, "leaf_wet_min": None,
                     "source": "open-meteo", "is_forecast": ts > datetime.now(), "fetched_at": datetime.now()})
    return rows


@pytest.fixture
def Session(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        load_seeds(s)
    yield factory
    engine.dispose()


@pytest.fixture
def seeded(Session):
    """Thanjavur has wet nights, Madurai is dry; the forecast was fetched just now."""
    with Session() as s:
        thanjavur = s.query(District).filter_by(name="Thanjavur").one()
        madurai = s.query(District).filter_by(name="Madurai").one()
        upsert(s, WeatherHourly, synthetic_weather(thanjavur.id, True) + synthetic_weather(madurai.id, False),
               ["district_id", "ts", "source"])
        upsert(s, Production, [
            {"area_code": 100, "area": "India", "item_code": 27, "item": "Rice", "year": 2024,
             "area_ha": 5.05e7, "production_t": 2.18e8, "yield_kg_ha": 4313.2, "source": "faostat"},
        ], ["area_code", "item_code", "year", "source"])
        s.add(IngestRun(source="open-meteo", started_at=datetime.now(), finished_at=datetime.now(),
                        status="ok", rows=1))
        s.commit()
        compute_risk(s)
    return Session


@pytest.fixture
def client(seeded):
    with TestClient(create_app(session_factory=seeded)) as c:
        yield c
