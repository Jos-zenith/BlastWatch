"""Retries, provider fallback, freshness, ingest logging and schema upgrades."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, inspect, select, text

from blastwatch.db import init_db
from blastwatch.ingest import weather
from blastwatch.models import District, IngestRun, WeatherHourly
from blastwatch.risk import load_rules
from blastwatch.runs import freshness

RULES = load_rules()

MET_NO = {"properties": {"timeseries": [
    {"time": "2025-11-10T00:00:00Z", "data": {
        "instant": {"details": {"air_temperature": 24.5, "relative_humidity": 96.0,
                                "dew_point_temperature": 23.8, "cloud_area_fraction": 90.0}},
        "next_1_hours": {"details": {"precipitation_amount": 0.4}}}},
    {"time": "2025-11-13T00:00:00Z", "data": {  # 6-hourly step: no next_1_hours
        "instant": {"details": {"air_temperature": 26.0}},
        "next_6_hours": {"details": {"precipitation_amount": 2.0}}}},
]}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(weather.time, "sleep", lambda s: None)


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_get_json_retries_server_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": True})

    assert weather._get_json("https://x.test/a", {}, mock_client(handler)) == {"ok": True}
    assert len(calls) == 3


def test_get_json_gives_up_and_does_not_retry_client_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400)

    with pytest.raises(httpx.HTTPStatusError):
        weather._get_json("https://x.test/a", {}, mock_client(handler))
    assert len(calls) == 1


def test_met_no_parse_converts_utc_to_ist_and_skips_coarse_steps():
    [row] = weather.parse_met_no(MET_NO, 3, datetime(2025, 11, 9))
    assert row["ts"] == datetime(2025, 11, 10, 5)  # 00:00Z = 05:30 IST, floored
    assert row["dew_point_c"] == 23.8 and row["precip_mm"] == 0.4
    assert row["source"] == "met-no" and row["is_forecast"]


def test_forecast_falls_back_to_met_no_and_logs_both_runs(Session):
    def handler(request):
        if "open-meteo" in request.url.host:
            return httpx.Response(500)
        assert "blastwatch" in request.headers["User-Agent"]
        return httpx.Response(200, json=MET_NO)

    with Session() as s:
        count = weather.ingest_forecast(s, client=mock_client(handler))
        assert count == s.query(District).count()  # one hourly step per district
        runs = {r.source: r.status for r in s.scalars(select(IngestRun))}
        assert runs == {"open-meteo": "failed", "met-no": "ok"}
        assert s.scalar(select(WeatherHourly.source).limit(1)) == "met-no"
        assert freshness(s, RULES)["source"] == "met-no"


def open_meteo_point(n_hours: int = 1) -> dict:
    return {"hourly": {"time": ["2025-11-10T00:00"] * n_hours, "temperature_2m": [25.0],
                       "relative_humidity_2m": [95.0], "precipitation": [0.0], "cloud_cover": [80.0]}}


def test_open_meteo_weight_counts_every_location():
    assert weather.open_meteo_weight(1, days=10, n_vars=6) == 1
    assert weather.open_meteo_weight(100, days=10, n_vars=6) == 100  # batching does not save quota
    assert weather.open_meteo_weight(2, days=28, n_vars=15) == 6


def test_open_meteo_splits_districts_into_paced_chunks(monkeypatch):
    sleeps, sizes = [], []
    monkeypatch.setattr(weather.time, "sleep", sleeps.append)

    def handler(request):
        n = len(request.url.params["latitude"].split(","))
        sizes.append(n)
        return httpx.Response(200, json=[open_meteo_point()] * n if n > 1 else open_meteo_point())

    districts = [SimpleNamespace(id=i, lat=10 + i / 1000, lon=79.0) for i in range(201)]
    rows = weather.fetch_open_meteo(districts, client=mock_client(handler))
    assert sizes == [100, 100, 1]
    assert [r["district_id"] for r in rows] == list(range(201))
    # 100 calls billed per full chunk at 500/min: 12 s before each later chunk.
    assert sleeps == [12.0, 12.0]


def test_met_no_requests_are_throttled(monkeypatch):
    sleeps = []
    monkeypatch.setattr(weather.time, "sleep", sleeps.append)
    districts = [SimpleNamespace(id=i, lat=10.0, lon=79.0) for i in range(3)]
    rows = weather.fetch_met_no(districts, mock_client(lambda r: httpx.Response(200, json=MET_NO)))
    assert len(rows) == 3
    assert sleeps == [weather.MET_NO_MIN_INTERVAL] * 2


def test_forecast_raises_when_every_source_fails(Session):
    with Session() as s, pytest.raises(httpx.HTTPStatusError):
        weather.ingest_forecast(s, client=mock_client(lambda r: httpx.Response(502)))
    with Session() as s:
        assert [r.status for r in s.scalars(select(IngestRun))] == ["failed", "failed"]
        assert freshness(s, RULES)["last_failure"]["source"] == "met-no"


@pytest.mark.parametrize("age_h, status", [(1, "fresh"), (10, "stale"), (30, "expired")])
def test_freshness_policy(Session, age_h, status):
    now = datetime(2025, 11, 10, 12)
    with Session() as s:
        s.add(IngestRun(source="open-meteo", started_at=now - timedelta(hours=age_h),
                        finished_at=now - timedelta(hours=age_h), status="ok", rows=1))
        s.commit()
        assert freshness(s, RULES, now)["status"] == status


def test_freshness_without_any_run_is_expired(Session):
    with Session() as s:
        assert freshness(s, RULES)["status"] == "expired"


def test_init_db_adds_columns_missing_from_an_old_database(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with engine.begin() as conn:  # weather_hourly as created by v0.1, before dew point / leaf wetness
        conn.execute(text("""CREATE TABLE weather_hourly (id INTEGER PRIMARY KEY, district_id INTEGER,
            ts DATETIME, temp_c FLOAT, rh_pct FLOAT, precip_mm FLOAT, cloud_pct FLOAT,
            source VARCHAR(20), is_forecast BOOLEAN, fetched_at DATETIME,
            UNIQUE (district_id, ts, source))"""))
    added = init_db(engine)
    assert {"weather_hourly.dew_point_c", "weather_hourly.leaf_wet_prob", "weather_hourly.leaf_wet_min"} <= set(added)
    assert "dew_point_c" in {c["name"] for c in inspect(engine).get_columns("weather_hourly")}
    assert init_db(engine) == []  # idempotent
