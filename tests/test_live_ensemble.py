import asyncio
import json
import time
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import func, select

from blastwatch import config, ensemble
from blastwatch.events import Bus, bus, sse
from blastwatch.models import District, EnsembleDaily, RiskDaily, WeatherHourly
from blastwatch.risk import load_rules
from blastwatch.sim import VirtualStation, reading, replay_hours


def ids(client):
    return {d["name"]: d["id"] for d in client.get("/api/districts").json()}


# ---- Event bus ------------------------------------------------------------------------------

def test_bus_delivers_across_threads_and_keeps_history():
    b = Bus(history=3)

    async def scenario():
        loop, queue = b.subscribe()
        await asyncio.to_thread(b.publish, "risk.changed", day=date(2026, 10, 9))
        event = await asyncio.wait_for(queue.get(), 1)
        b.unsubscribe(loop, queue)
        return event

    event = asyncio.run(scenario())
    assert event["kind"] == "risk.changed" and event["data"]["day"] == "2026-10-09"
    for i in range(5):
        b.publish("x", i=i)
    assert [e["data"]["i"] for e in b.since(0)] == [2, 3, 4]  # history is bounded
    assert b.since(b.since(0)[-1]["id"]) == []
    assert b.listeners == 0
    frame = sse(event)
    assert frame.startswith(f"id: {event['id']}\ndata: ") and json.loads(frame.split("data: ", 1)[1])["kind"] == "risk.changed"


# ---- Ensemble -------------------------------------------------------------------------------

def ensemble_payload(wet_members: int, members: int = 10) -> dict:
    """Hourly columns for a control run plus member01..; the first `wet_members` have saturated
    nights (leaf-wetness probability 95 % from 20:00 to 09:59), the rest stay dry."""
    start = datetime.combine(date.today() - timedelta(days=1), datetime.min.time())
    times = [start + timedelta(hours=i) for i in range(24 * 8)]
    hourly = {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in times]}
    for m in range(members):
        suffix = "" if m == 0 else f"_member{m:02d}"
        wet = m < wet_members
        hourly[f"temperature_2m{suffix}"] = [25.0] * len(times)
        hourly[f"relative_humidity_2m{suffix}"] = [70.0] * len(times)
        hourly[f"dew_point_2m{suffix}"] = [19.0] * len(times)
        hourly[f"precipitation{suffix}"] = [0.0] * len(times)
        hourly[f"cloud_cover{suffix}"] = [90.0] * len(times)
        hourly[f"leaf_wetness_probability{suffix}"] = [
            95.0 if wet and (t.hour >= 20 or t.hour < 10) else 10.0 for t in times]
    return {"hourly": hourly}


def test_parse_and_score_members():
    rules = load_rules()
    members = ensemble.parse_members(ensemble_payload(wet_members=3, members=10), datetime.now())
    assert len(members) == 10 and "00" in members and "09" in members
    by_day = ensemble.score_members(members, rules, "Tamil Nadu", date.today())
    tomorrow = by_day[date.today() + timedelta(days=1)]
    s = ensemble.summarise(tomorrow["scores"], tomorrow["levels"], tomorrow["wet"])
    assert s["members"] == 10 and s["p_high"] == 0.3
    assert s["score_p10"] <= s["score_p50"] <= s["score_p90"]


def test_pooled_weights_each_model_equally():
    a = {"members": 51, "p_high": 0.9, "p_moderate": 0.1, "score_p10": 60, "score_p50": 80, "score_p90": 100,
         "score_mean": 80, "wet_hours_p50": 14}
    b = {**a, "members": 31, "p_high": 0.0, "p_moderate": 0.0, "score_p10": 0, "score_p50": 10, "score_p90": 20,
         "score_mean": 10, "wet_hours_p50": 2}
    p = ensemble.pooled({"ecmwf": a, "gefs": b})
    assert p["members"] == 82 and p["p_high"] == 0.45  # not 51/82-weighted
    assert p["score_p10"] == 0 and p["score_p90"] == 100


def test_ensemble_action_rule():
    rules, today = load_rules(), date.today()
    day = lambda i, p: {"date": today + timedelta(days=i), "p_high": p, "p_moderate": 0.0}
    assert ensemble.ensemble_action([day(0, 0.6), day(1, 0.5), day(2, 0.1)], rules, today) == "alert"
    assert ensemble.ensemble_action([day(0, 0.6), day(1, 0.1), day(2, 0.1)], rules, today) == "watch"
    assert ensemble.ensemble_action([day(0, 0.0), day(1, 0.05), day(4, 0.1)], rules, today) == "none"
    assert ensemble.ensemble_action([], rules, today) is None


def test_ensemble_ingest_and_api(client, seeded):
    wet = {"ecmwf_ifs025": 8, "gfs025": 0, "icon_seamless": 4}  # out of 10 members each

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(request.url.params["latitude"].split(","))
        return httpx.Response(200, json=[ensemble_payload(wet[request.url.params["models"]])] * n)

    events_before = bus.since(0)[-1]["id"] if bus.since(0) else 0
    with seeded() as s, httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = ensemble.ingest(s, load_rules(), client=http, pace=False)
        assert result["models"] == ["ecmwf", "gefs", "icon"]
        assert s.scalar(select(func.count(EnsembleDaily.id))) == result["rows"] > 0
    assert "ensemble.updated" in [e["kind"] for e in bus.since(events_before)]

    tid = ids(client)["Thanjavur"]
    body = client.get("/api/ensemble").json()
    assert body["run_at"] and set(body["models"]) == {"ecmwf", "gefs", "icon"}
    thanjavur = next(d for d in body["districts"] if d["id"] == tid)
    tomorrow = next(d for d in thanjavur["days"] if d["date"] == (date.today() + timedelta(days=1)).isoformat())
    assert tomorrow["models"]["ecmwf"]["p_high"] == 0.8 and tomorrow["models"]["gefs"]["p_high"] == 0.0
    assert tomorrow["p_high"] == 0.4  # (0.8 + 0.0 + 0.4) / 3
    assert tomorrow["level"] == "High"  # rule-based level from the seeded live forecast
    outlook = next(d for d in client.get("/api/outlook").json()["districts"] if d["id"] == tid)
    assert any(d["p_high"] == 0.4 and d["ens_members"] == 30 for d in outlook["days"])


def test_ensemble_skips_a_failing_model(seeded):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["models"] == "gfs025":
            return httpx.Response(400, json={"reason": "bad"})
        n = len(request.url.params["latitude"].split(","))
        return httpx.Response(200, json=[ensemble_payload(5)] * n)

    with seeded() as s, httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = ensemble.ingest(s, load_rules(), client=http, pace=False)
        assert result["models"] == ["ecmwf", "icon"]
        pooled = s.scalars(select(EnsembleDaily).where(EnsembleDaily.model == "all")).first()
        assert pooled.members == 20


# ---- Sensors and the virtual station ---------------------------------------------------------

def test_sensor_upload_rescores_and_publishes(client, monkeypatch):
    monkeypatch.setattr(config, "INGEST_KEY", "k")
    mid = ids(client)["Madurai"]  # dry in the seeded forecast
    start = replay_hours(datetime.now())[0]
    readings = [{"ts": (start + timedelta(hours=i)).isoformat(), "temp_c": 24.0, "rh_pct": 98,
                 "leaf_wet_min": 60 if i >= 6 else 0} for i in range(24)]
    last = bus.since(0)[-1]["id"] if bus.since(0) else 0
    r = client.post("/api/sensors/readings", headers={"X-API-Key": "k"},
                    json={"station_id": "TNJ-01", "district": "Madurai", "readings": readings})
    assert r.status_code == 202
    body = r.json()
    assert body["day"]["wetness_basis"] == "sensor" and body["day"]["level"] == "High"
    assert any(c["district_id"] == mid and c["to"] == "High" for c in body["changes"])
    kinds = [e["kind"] for e in bus.since(last)]
    assert "sensor.reading" in kinds and "risk.changed" in kinds


def test_scenarios_shape_the_night():
    rules = load_rules()
    from random import Random

    from blastwatch.risk import HourObs, score_window

    start = datetime.combine(date.today() - timedelta(days=1), datetime.min.time()).replace(hour=12)
    hours = [start + timedelta(hours=i) for i in range(24)]
    levels = {}
    for scenario in ("dew", "showers", "dry"):
        rng, obs = Random(1), []
        for ts in hours:
            r = reading(scenario, ts, rng)
            obs.append(HourObs(ts, r["temp_c"], r["rh_pct"], r["precip_mm"], leaf_wet_min=r["leaf_wet_min"]))
        levels[scenario] = score_window(obs, rules, 1.0, date.today()).level
    assert levels == {"dew": "High", "showers": "Moderate", "dry": "Low"}


def test_virtual_station_runs_and_resets(seeded):
    station = VirtualStation(seeded)
    with seeded() as s:
        madurai = s.scalar(select(District).where(District.name == "Madurai"))
    station.start(madurai.id, "dew", seconds_per_hour=0, seed=3)
    deadline = time.time() + 20
    while station.state.running and time.time() < deadline:
        time.sleep(0.05)
    assert not station.state.running and station.state.step == station.state.total > 0
    assert station.state.last["day"]["wetness_basis"] == "sensor"
    with seeded() as s:
        assert s.scalar(select(func.count(WeatherHourly.id)).where(WeatherHourly.source.like("sensor:SIM%")))
        today = s.scalar(select(RiskDaily).where(RiskDaily.district_id == madurai.id, RiskDaily.date == date.today()))
        assert today.level == "High"
    result = station.reset()
    assert result["districts"] == 1
    with seeded() as s:
        assert not s.scalar(select(func.count(WeatherHourly.id)).where(WeatherHourly.source.like("sensor:SIM%")))
        today = s.scalar(select(RiskDaily).where(RiskDaily.district_id == madurai.id, RiskDaily.date == date.today()))
        assert today.level == "Low"


def test_demo_endpoints_need_the_flag(client, monkeypatch):
    monkeypatch.setattr(config, "DEMO", False)
    assert client.post("/api/sim/start", json={"district_id": 1}).status_code == 503
    assert client.post("/api/refresh").status_code == 503
    status = client.get("/api/live/status").json()
    assert status["demo"] is False and status["sim"]["running"] is False and "dew" in status["scenarios"]
    monkeypatch.setattr(config, "DEMO", True)
    assert client.post("/api/sim/start", json={"district_id": 9999}).status_code == 404
    assert client.post("/api/sim/reset").json()["districts"] == 0


def test_recent_events_endpoint(client):
    bus.publish("test.ping", n=1)
    events = client.get("/api/events?limit=5").json()
    assert events[-1]["kind"] == "test.ping" and len(events) <= 5
