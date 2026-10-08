"""Virtual field station, for demonstrations and for testing the live path end to end.

It replays last night's infection window (yesterday 12:00 up to now, at most until today 11:00)
for one district at a chosen speed. Each simulated hour goes through the same ingest as a real
sensor (sensors.ingest_readings), so the district is rescored, dashboards update over the event
stream, and at the end the alert rule runs as it does after a refresh.

Simulated rows use station ids "SIM<district id>" (source "sensor:SIM.."), and `reset` deletes them
and rescores, so a demo leaves no trace in the data used for calibration. The endpoints are only
enabled with BLASTWATCH_DEMO=1: anyone who can reach them can change what officers see.
"""
import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import alerts
from .events import publish
from .models import District, WeatherHourly
from .pipeline import compute_risk, level_changes, level_snapshot
from .risk import load_rules
from .sensors import ingest_readings

log = logging.getLogger("blastwatch.sim")
SIM_PREFIX = "SIM"

SCENARIOS = {
    "dew": "Long dew night: leaves wet from 18:00 to 10:00 at 23-25 °C (blast-favourable)",
    "showers": "Showers from 16:00, a dry break 02:00-04:00, dew again before dawn",
    "dry": "Clear, dry night: humidity stays below 85 %, leaves stay dry",
}


def station_id(district_id: int) -> str:
    return f"{SIM_PREFIX}{district_id:02d}"


def reading(scenario: str, ts: datetime, rng: random.Random) -> dict:
    """One hour of simulated sensor values for a scenario."""
    h = ts.hour
    hours_after_dusk = (h - 18) % 24  # 0 at 18:00
    night_temp = 25.5 - 0.18 * hours_after_dusk + rng.uniform(-0.3, 0.3)
    day_temp = 30.5 + 1.5 * (1 - abs(h - 14) / 4) + rng.uniform(-0.4, 0.4)
    dry = {"temp_c": round(day_temp, 1), "rh_pct": round(rng.uniform(62, 74)), "leaf_wet_min": 0, "precip_mm": 0.0}
    if scenario == "dew":
        if h >= 18 or h < 10:
            return {"temp_c": round(night_temp, 1), "rh_pct": round(rng.uniform(96, 99.5), 1),
                    "leaf_wet_min": 45 if h == 18 else 60, "precip_mm": 0.0}
        return dry
    if scenario == "showers":
        if 16 <= h or h < 2:
            return {"temp_c": round(min(night_temp, 26.5), 1), "rh_pct": round(rng.uniform(94, 99), 1),
                    "leaf_wet_min": 60, "precip_mm": round(rng.uniform(0.4, 1.2), 1) if h in (16, 17, 18) else 0.0}
        if 2 <= h < 4:
            return {"temp_c": round(night_temp, 1), "rh_pct": round(rng.uniform(82, 88)), "leaf_wet_min": 0,
                    "precip_mm": 0.0}
        if 4 <= h < 9:
            return {"temp_c": round(night_temp, 1), "rh_pct": round(rng.uniform(94, 98), 1), "leaf_wet_min": 60,
                    "precip_mm": 0.0}
        return dry
    if scenario == "dry":
        if h >= 18 or h < 9:
            return {"temp_c": round(night_temp - 1, 1), "rh_pct": round(rng.uniform(70, 84)), "leaf_wet_min": 0,
                    "precip_mm": 0.0}
        return dry
    raise ValueError(f"unknown scenario {scenario!r}")


def replay_hours(now: datetime, window_start_hour: int = 12) -> list[datetime]:
    """Today's infection window, from yesterday at the window start up to the current hour."""
    start = datetime.combine(now.date() - timedelta(days=1), datetime.min.time()).replace(hour=window_start_hour)
    end = min(now.replace(minute=0, second=0, microsecond=0), start + timedelta(hours=23))
    return [start + timedelta(hours=i) for i in range(int((end - start).total_seconds() // 3600) + 1)]


@dataclass
class SimState:
    running: bool = False
    district: str | None = None
    scenario: str | None = None
    step: int = 0
    total: int = 0
    started_at: datetime | None = None
    last: dict | None = None
    stop: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict:
        return {"running": self.running, "district": self.district, "scenario": self.scenario,
                "step": self.step, "total": self.total, "started_at": self.started_at, "last": self.last}


class VirtualStation:
    """One simulation at a time, run in a background thread."""

    def __init__(self, session_factory: Callable[[], Session]):
        self.session_factory = session_factory
        self.state = SimState()
        self._lock = threading.Lock()

    def start(self, district_id: int, scenario: str, seconds_per_hour: float = 1.5, seed: int | None = None) -> dict:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}")
        with self._lock:
            if self.state.running:
                raise RuntimeError("a simulation is already running")
            with self.session_factory() as s:
                district = s.get(District, district_id)
                if district is None:
                    raise LookupError("district not found")
            rules = load_rules()
            hours = replay_hours(datetime.now(), rules.get("window_start_hour", 12))
            self.state = SimState(running=True, district=district.name, scenario=scenario, total=len(hours),
                                  started_at=datetime.now())
            thread = threading.Thread(target=self._run, name="virtual-station", daemon=True,
                                      args=(district_id, scenario, hours, seconds_per_hour, seed, rules))
            thread.start()
        publish("sim.started", district=district.name, district_id=district_id, scenario=scenario,
                description=SCENARIOS[scenario], hours=len(hours), station=station_id(district_id))
        return self.state.public()

    def _run(self, district_id, scenario, hours, seconds_per_hour, seed, rules) -> None:
        rng = random.Random(seed)
        state = self.state
        try:
            with self.session_factory() as s:
                district = s.get(District, district_id)
                for i, ts in enumerate(hours):
                    if state.stop.is_set():
                        break
                    values = {"ts": ts, **reading(scenario, ts, rng)}
                    result = ingest_readings(s, district, station_id(district_id), [values], rules)
                    state.step, state.last = i + 1, {"reading": result["reading"], "day": result["day"]}
                    if state.stop.wait(seconds_per_hour):
                        break
                alert_result = alerts.evaluate(s, rules)
            publish("alerts.evaluated", cause=f"virtual station {station_id(district_id)}", **alert_result)
            publish("sim.done", district=state.district, scenario=scenario, steps=state.step,
                    stopped=state.stop.is_set(), day=(state.last or {}).get("day"))
        except Exception as exc:
            log.exception("virtual station failed")
            publish("sim.failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            state.running = False

    def stop(self) -> dict:
        self.state.stop.set()
        return self.state.public()

    def reset(self) -> dict:
        """Delete every simulated reading, rescore and re-run the alert rule."""
        if self.state.running:
            raise RuntimeError("stop the simulation first")
        rules = load_rules()
        with self.session_factory() as s:
            affected = list(s.scalars(select(WeatherHourly.district_id).where(
                WeatherHourly.source.like(f"sensor:{SIM_PREFIX}%")).distinct()))
            since = date.today() - timedelta(days=1)
            before = level_snapshot(s, rules, since, affected)
            s.execute(delete(WeatherHourly).where(WeatherHourly.source.like(f"sensor:{SIM_PREFIX}%")))
            s.commit()
            if affected:
                compute_risk(s, rules, affected)
            changes = level_changes(s, before, level_snapshot(s, rules, since, affected))
            alert_result = alerts.evaluate(s, rules)
        publish("sim.reset", districts=len(affected))
        if changes:
            publish("risk.changed", cause="virtual station reset", changes=changes)
        publish("alerts.evaluated", cause="virtual station reset", **alert_result)
        return {"districts": len(affected), "changes": changes, "alerts": alert_result}
