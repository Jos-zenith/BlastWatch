"""Hourly weather.

- Open-Meteo: primary live/forecast source (includes a modelled leaf-wetness probability).
  Districts are batched into multi-location requests, paced to stay under the per-minute limit.
- MET Norway: fallback forecast when Open-Meteo is down (about 2.5 days of hourly steps).
  It has no multi-location API, so requests go one district at a time, throttled.
- NASA POWER: historical backfill for calibration.

Every request retries on timeouts, 429 and 5xx with exponential backoff. Every forecast
attempt is logged in `ingest_run`, which drives the freshness status shown to users.
"""
import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from .. import config
from ..db import upsert
from ..models import District, WeatherHourly
from ..runs import track

log = logging.getLogger("blastwatch.weather")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
MET_NO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
NASA_POWER_URL = "https://power.larc.nasa.gov/api/temporal/hourly/point"
HOURLY_VARS = ("temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,cloud_cover,"
               "leaf_wetness_probability")
POWER_FILL = -999.0
IST = timezone(timedelta(hours=5, minutes=30))
RETRY_STATUS = {429, 500, 502, 503, 504}
KEYS = ["district_id", "ts", "source"]
# Batching saves round trips, not quota: Open-Meteo counts every location as at least one call.
OPEN_METEO_MAX_LOCATIONS = 100  # keeps the GET URL around 2 KB
OPEN_METEO_CALLS_PER_MIN = 500  # free tier allows 600/min; the rest is headroom for retries
MET_NO_MIN_INTERVAL = 0.1  # seconds between requests; MET Norway allows 20 req/s per application


def _get_json(url: str, params: dict, client: httpx.Client | None, attempts: int = 3,
              backoff: float = 2.0, headers: dict | None = None):
    if client is None:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as own:
            return _get_json(url, params, own, attempts, backoff, headers)
    for attempt in range(1, attempts + 1):
        try:
            resp = client.get(url, params=params, headers=headers)
            if resp.status_code not in RETRY_STATUS or attempt == attempts:
                resp.raise_for_status()
                return resp.json()
            wait = float(resp.headers.get("Retry-After", backoff * 2 ** (attempt - 1)))
        except httpx.TransportError:
            if attempt == attempts:
                raise
            wait = backoff * 2 ** (attempt - 1)
        log.warning("GET %s failed (attempt %s/%s), retrying in %.0fs", url, attempt, attempts, wait)
        time.sleep(min(wait, 30))


def _row(district_id, ts, now, source, is_forecast, **values) -> dict:
    row = {"district_id": district_id, "ts": ts, "source": source, "is_forecast": is_forecast,
           "fetched_at": now, "temp_c": None, "rh_pct": None, "precip_mm": None, "cloud_pct": None,
           "dew_point_c": None, "leaf_wet_prob": None, "leaf_wet_min": None}
    row.update(values)
    return row


def parse_open_meteo(payload: dict, district_id: int, now: datetime) -> list[dict]:
    h = payload["hourly"]
    rows = []
    for i, ts_text in enumerate(h["time"]):
        ts = datetime.fromisoformat(ts_text)
        rows.append(_row(
            district_id, ts, now, "open-meteo", ts > now,
            temp_c=h["temperature_2m"][i],
            rh_pct=h["relative_humidity_2m"][i],
            dew_point_c=h.get("dew_point_2m", [None] * len(h["time"]))[i],
            precip_mm=h["precipitation"][i],
            cloud_pct=h["cloud_cover"][i],
            leaf_wet_prob=h.get("leaf_wetness_probability", [None] * len(h["time"]))[i],
        ))
    return rows


def open_meteo_weight(n_locations: int, days: int, n_vars: int) -> float:
    """API calls Open-Meteo bills for one request: locations x max(1, days/14) x max(1, vars/10)."""
    return n_locations * max(1.0, days / 14) * max(1.0, n_vars / 10)


def fetch_open_meteo(
    districts: list[District], past_days: int = 3, forecast_days: int = 7, client: httpx.Client | None = None
) -> list[dict]:
    """Multi-location requests of up to OPEN_METEO_MAX_LOCATIONS districts each.

    After each request, wait long enough that the calls it was billed for stay within
    OPEN_METEO_CALLS_PER_MIN. A single request covers all 17 pilot districts, so there is no wait.
    """
    if not districts:
        return []
    if client is None:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as own:
            return fetch_open_meteo(districts, past_days, forecast_days, own)
    n_vars = len(HOURLY_VARS.split(","))
    rows = []
    for start in range(0, len(districts), OPEN_METEO_MAX_LOCATIONS):
        chunk = districts[start:start + OPEN_METEO_MAX_LOCATIONS]
        if start:
            spent = open_meteo_weight(OPEN_METEO_MAX_LOCATIONS, past_days + forecast_days, n_vars)
            time.sleep(60 * spent / OPEN_METEO_CALLS_PER_MIN)
        params = {
            "latitude": ",".join(str(d.lat) for d in chunk),
            "longitude": ",".join(str(d.lon) for d in chunk),
            "hourly": HOURLY_VARS,
            "timezone": config.TIMEZONE,
            "past_days": past_days,
            "forecast_days": forecast_days,
        }
        payload = _get_json(OPEN_METEO_URL, params, client)
        payloads = payload if isinstance(payload, list) else [payload]
        now = datetime.now()
        for district, item in zip(chunk, payloads, strict=True):
            rows.extend(parse_open_meteo(item, district.id, now))
    return rows


def parse_met_no(payload: dict, district_id: int, now: datetime) -> list[dict]:
    """Keep only hourly steps; times are UTC and land on :30 in IST, so floor to the hour."""
    rows = []
    for step in payload["properties"]["timeseries"]:
        hourly = step["data"].get("next_1_hours")
        if hourly is None:
            continue  # beyond ~60 h MET Norway switches to 6-hourly steps
        utc = datetime.fromisoformat(step["time"].replace("Z", "+00:00"))
        ts = utc.astimezone(IST).replace(tzinfo=None, minute=0)
        d = step["data"]["instant"]["details"]
        rows.append(_row(
            district_id, ts, now, "met-no", ts > now,
            temp_c=d.get("air_temperature"),
            rh_pct=d.get("relative_humidity"),
            dew_point_c=d.get("dew_point_temperature"),
            cloud_pct=d.get("cloud_area_fraction"),
            precip_mm=hourly.get("details", {}).get("precipitation_amount"),
        ))
    return rows


def fetch_met_no(districts: list[District], client: httpx.Client | None = None) -> list[dict]:
    if client is None:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as own:
            return fetch_met_no(districts, own)
    now = datetime.now()
    headers = {"User-Agent": config.USER_AGENT}
    rows = []
    for i, d in enumerate(districts):
        if i:
            time.sleep(MET_NO_MIN_INTERVAL)
        params = {"lat": round(d.lat, 4), "lon": round(d.lon, 4)}
        rows.extend(parse_met_no(_get_json(MET_NO_URL, params, client, headers=headers), d.id, now))
    return rows


def parse_nasa_power(payload: dict, district_id: int, now: datetime) -> list[dict]:
    p = payload["properties"]["parameter"]

    def clean(value):
        return None if value is None or value <= POWER_FILL else value

    return [
        _row(
            district_id, datetime.strptime(key, "%Y%m%d%H"), now, "nasa-power", False,
            temp_c=clean(temp),
            rh_pct=clean(p["RH2M"].get(key)),
            dew_point_c=clean(p.get("T2MDEW", {}).get(key)),
            precip_mm=clean(p["PRECTOTCORR"].get(key)),
        )
        for key, temp in p["T2M"].items()
    ]


def fetch_nasa_power(district: District, start: date, end: date, client: httpx.Client | None = None) -> list[dict]:
    params = {
        "parameters": "T2M,RH2M,T2MDEW,PRECTOTCORR",
        "community": "AG",
        "latitude": district.lat,
        "longitude": district.lon,
        "start": start.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "format": "JSON",
        "time-standard": "LST",
    }
    return parse_nasa_power(_get_json(NASA_POWER_URL, params, client), district.id, datetime.now())


def ingest_forecast(session, past_days: int = 3, forecast_days: int = 7, client: httpx.Client | None = None) -> int:
    """Open-Meteo first; MET Norway if it fails. Raises only if both fail."""
    districts = session.query(District).order_by(District.id).all()
    try:
        with track(session, "open-meteo") as run:
            run.rows = upsert(session, WeatherHourly, fetch_open_meteo(districts, past_days, forecast_days, client), KEYS)
        return run.rows
    except Exception:
        log.exception("Open-Meteo failed; falling back to MET Norway")
    with track(session, "met-no") as run:
        run.rows = upsert(session, WeatherHourly, fetch_met_no(districts, client), KEYS)
    return run.rows


def ingest_backfill(session, start: date, end: date) -> int:
    count = 0
    with httpx.Client(timeout=config.HTTP_TIMEOUT) as client, track(session, "nasa-power") as run:
        for district in session.query(District).order_by(District.id):
            count += upsert(session, WeatherHourly, fetch_nasa_power(district, start, end, client), KEYS)
            session.commit()
        run.rows = count
    return count
