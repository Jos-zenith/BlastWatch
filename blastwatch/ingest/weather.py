"""Hourly weather: Open-Meteo for live/forecast data, NASA POWER for historical backfill."""
from datetime import date, datetime

import httpx

from .. import config
from ..db import upsert
from ..models import District, WeatherHourly

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
NASA_POWER_URL = "https://power.larc.nasa.gov/api/temporal/hourly/point"
HOURLY_VARS = "temperature_2m,relative_humidity_2m,precipitation,cloud_cover"
POWER_FILL = -999.0


def _get_json(url: str, params: dict, client: httpx.Client | None):
    if client is None:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as own:
            return _get_json(url, params, own)
    resp = client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def parse_open_meteo(payload: dict, district_id: int, now: datetime) -> list[dict]:
    h = payload["hourly"]
    rows = []
    for i, ts_text in enumerate(h["time"]):
        ts = datetime.fromisoformat(ts_text)
        rows.append(
            {
                "district_id": district_id,
                "ts": ts,
                "temp_c": h["temperature_2m"][i],
                "rh_pct": h["relative_humidity_2m"][i],
                "precip_mm": h["precipitation"][i],
                "cloud_pct": h["cloud_cover"][i],
                "source": "open-meteo",
                "is_forecast": ts > now,
                "fetched_at": now,
            }
        )
    return rows


def fetch_open_meteo(
    districts: list[District], past_days: int = 3, forecast_days: int = 7, client: httpx.Client | None = None
) -> list[dict]:
    """One batched request for all districts (Open-Meteo accepts comma-separated coordinates)."""
    if not districts:
        return []
    params = {
        "latitude": ",".join(str(d.lat) for d in districts),
        "longitude": ",".join(str(d.lon) for d in districts),
        "hourly": HOURLY_VARS,
        "timezone": config.TIMEZONE,
        "past_days": past_days,
        "forecast_days": forecast_days,
    }
    payload = _get_json(OPEN_METEO_URL, params, client)
    payloads = payload if isinstance(payload, list) else [payload]

    now = datetime.now()
    rows = []
    for district, item in zip(districts, payloads, strict=True):
        rows.extend(parse_open_meteo(item, district.id, now))
    return rows


def parse_nasa_power(payload: dict, district_id: int, now: datetime) -> list[dict]:
    p = payload["properties"]["parameter"]

    def clean(value):
        return None if value is None or value <= POWER_FILL else value

    rows = []
    for key, temp in p["T2M"].items():
        rows.append(
            {
                "district_id": district_id,
                "ts": datetime.strptime(key, "%Y%m%d%H"),
                "temp_c": clean(temp),
                "rh_pct": clean(p["RH2M"].get(key)),
                "precip_mm": clean(p["PRECTOTCORR"].get(key)),
                "cloud_pct": None,
                "source": "nasa-power",
                "is_forecast": False,
                "fetched_at": now,
            }
        )
    return rows


def fetch_nasa_power(district: District, start: date, end: date, client: httpx.Client | None = None) -> list[dict]:
    params = {
        "parameters": "T2M,RH2M,PRECTOTCORR",
        "community": "AG",
        "latitude": district.lat,
        "longitude": district.lon,
        "start": start.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "format": "JSON",
        "time-standard": "LST",
    }
    return parse_nasa_power(_get_json(NASA_POWER_URL, params, client), district.id, datetime.now())


def ingest_forecast(session, past_days: int = 3, forecast_days: int = 7) -> int:
    districts = session.query(District).order_by(District.id).all()
    rows = fetch_open_meteo(districts, past_days, forecast_days)
    count = upsert(session, WeatherHourly, rows, ["district_id", "ts", "source"])
    session.commit()
    return count


def ingest_backfill(session, start: date, end: date) -> int:
    count = 0
    with httpx.Client(timeout=config.HTTP_TIMEOUT) as client:
        for district in session.query(District).order_by(District.id):
            rows = fetch_nasa_power(district, start, end, client)
            count += upsert(session, WeatherHourly, rows, ["district_id", "ts", "source"])
            session.commit()
    return count
