"""Open-Meteo client. Free, no API key. Gridded model data, not in-canopy readings."""
from datetime import datetime, timezone

import httpx

FORECAST = "https://api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m"


def _rows(payload: dict) -> list[dict]:
    h = payload["hourly"]
    return [
        # Open-Meteo wind is km/h by default; convert to m/s
        {"ts": t, "temp": T, "rh": rh, "precip": p, "wind": (w / 3.6 if w is not None else None)}
        for t, T, rh, p, w in zip(
            h["time"], h["temperature_2m"], h["relative_humidity_2m"], h["precipitation"], h["wind_speed_10m"]
        )
    ]


def fetch_forecast(lat: float, lon: float, days: int = 7, client: httpx.Client | None = None) -> list[dict]:
    c = client or httpx.Client(timeout=30)
    r = c.get(FORECAST, params={"latitude": lat, "longitude": lon, "hourly": HOURLY,
                                "forecast_days": days, "past_days": 2, "timezone": "Asia/Kolkata"})
    r.raise_for_status()
    return _rows(r.json())


def fetch_archive(lat: float, lon: float, start: str, end: str, client: httpx.Client | None = None) -> list[dict]:
    c = client or httpx.Client(timeout=60)
    r = c.get(ARCHIVE, params={"latitude": lat, "longitude": lon, "hourly": HOURLY,
                               "start_date": start, "end_date": end, "timezone": "Asia/Kolkata"})
    r.raise_for_status()
    return _rows(r.json())


def store(con, block_id: str, rows: list[dict], source: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.executemany(
        "INSERT OR REPLACE INTO weather_hourly(block_id,ts,temp,rh,precip,wind,source,fetched_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [(block_id, r["ts"], r["temp"], r["rh"], r["precip"], r["wind"], source, now) for r in rows],
    )
    con.commit()
