# BlastWatch

Rice blast early warning for field officers. It combines an **agricultural database**
(FAOSTAT), a **biological database** (GenBank) and **live weather** into one decision tool.
The pilot covers 17 rice-growing districts of Tamil Nadu.

## Who it is for

| User | What they get | Where |
|---|---|---|
| **Block / district agricultural officers, KVK staff** (primary) | Which districts need action now, a 7-day outlook, and a ready-made farmer message to review and forward | `/` (English / தமிழ், phone-friendly) |
| **Farmers** | A short SMS/WhatsApp text in Tamil + English, forwarded by their officer. Farmers never need the dashboard. | Alert messages |
| **Researchers / students** | FAOSTAT production trends, GenBank resistance genes, data-pipeline status | `/research.html` |

Farmer messages always say **scout first, spray only if lesions are found**. The model forecasts
*weather favourable for blast*, not outbreaks, so a forecast alone should never trigger spraying.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt

python -m blastwatch all          # tables, seeds, FAOSTAT, GenBank, forecast, risk, alerts
python -m blastwatch serve --with-scheduler   # http://127.0.0.1:8000
```

On startup the server creates/upgrades tables and syncs the seed CSVs. With `--with-scheduler` it also
loads FAOSTAT/GenBank if missing and refreshes forecast → risk → alerts immediately and every 3 h.
A fresh deployment (e.g. the Render `Procfile`) therefore fills itself.

### Commands

| Command | What it does |
|---|---|
| `init` | Create tables, add new nullable columns to an existing DB, load `blastwatch/seed/*.csv` |
| `refresh` | Forecast (Open-Meteo, MET Norway fallback) → risk → alerts |
| `weather` / `risk` / `alerts` | The three refresh steps individually |
| `faostat [--region Asia] [--refresh]` | Rice production from the FAOSTAT bulk download (cached in `data/raw/`) |
| `genes [--retmax 20]` | GenBank records for every seeded gene |
| `backfill --start YYYY-MM-DD --end YYYY-MM-DD` | Historical hourly weather from NASA POWER |
| `observations import FILE` | Field observations for calibration (see below) |
| `backtest --start … --end … [--lead 3]` | Score the model against observations, sweeping the High threshold |
| `serve [--with-scheduler]` / `schedule` | API + dashboards / refresh loop on its own |

### Configuration

Environment variables, or a `.env` file in the project root (`.env` and `.env.local` are git-ignored;
`.env.local` is **not** loaded automatically):

- `BLASTWATCH_DATABASE_URL` defaults to SQLite at `data/blastwatch.db`. A PostgreSQL URL
  (`postgresql://…` or `postgresql+psycopg://…`) is used for deployment.
- `NCBI_EMAIL`, `NCBI_API_KEY` identify you to NCBI. A key raises the limit from 3 to 10 requests/s.
- `BLASTWATCH_INGEST_KEY` is the shared secret for field-sensor uploads. The endpoint is disabled while unset.
- `BLASTWATCH_USER_AGENT` identifies you to MET Norway, whose terms require contact details.

## Risk model (rules-v2)

Defined in [config/risk_rules.toml](config/risk_rules.toml). You can tune it without code changes.

- **Leaf wetness, best signal available per hour:** field sensor (wet ≥ 30 min) → Open-Meteo
  modelled leaf-wetness probability (≥ 50 %) → dew-point depression (T − Td ≤ 2 °C, or rain) → RH ≥ 90 %.
  Each day records which signal it used (`wetness_basis`), and the officer view shows it.
- An hour is **conducive** when the leaf is wet and the temperature is 20–30 °C.
- Each day is scored over the **infection night** (12:00 → 12:00) so dew periods are not split at midnight.
- Score (0–100) = longest wet run (60) + total wet hours (20) + light rain (10) + overcast (10),
  × crop susceptibility for the state and month. Levels: Low < 35 ≤ Moderate < 65 ≤ High.

### Limits, stated plainly

- **Not yet calibrated.** No outbreak data has been loaded. On history the model separates dry and
  monsoon seasons (March 2024 all Low, November 2023 almost all High) but cannot yet say which
  wet-season weeks lead to outbreaks. It scores weather, not crop stage, variety or inoculum.
- **Grid weather is not canopy weather.** Inside a flooded rice canopy, humidity is usually higher than at
  2 m. The modelled leaf-wetness variable and dew-point method narrow the gap. Field sensors close it.
- **Independent check:** on 5 Oct 2026, Open-Meteo (leaf-wetness method) and MET Norway (dew-point method)
  both rated Thanjavur High for 6–7 Oct. The signal is a property of the weather, not one provider.

## Alerts

An alert is created only when **≥ 2 of the next 3 days are High** (forecast skill drops after ~3 days),
the forecast is **fresh**, and the district has had no alert in the last 5 days. Alerts are stored as
`ready` for an officer to review, copy (Tamil / English) and mark as sent. Nothing is sent
automatically yet; wiring an SMS/WhatsApp provider is a deliberate next step.

The Tamil text (UI and messages) was drafted for this prototype. **A Tamil-speaking agronomist
should review it** (e.g. TNAU or KVK extension staff) before it reaches farmers.

## Data freshness and reliability

- Pages read only the local database. Weather APIs are called by the background scheduler, never on a
  page request.
- Requests retry on timeouts, 429 and 5xx with exponential backoff (honouring `Retry-After`).
- If Open-Meteo fails, MET Norway is used (about 2.5 days of hourly data, dew-point wetness).
- Open-Meteo is fetched in multi-location requests of up to 100 districts. Batching saves round
  trips, not quota: Open-Meteo bills every location as at least one call, so requests after the first
  are paced to stay under 500 calls/min (the free limit is 600). The 17 pilot districts need one request.
  At ~730 districts a 3-hourly refresh is about 5,800 calls/day, within the 10,000/day free tier. That tier
  is non-commercial only, so a production deployment needs a paid key.
- MET Norway has no multi-location API, so the fallback fetches one district at a time, at most 10 per second.
- Every ingest attempt is logged (`ingest_run`). The forecast is **fresh** < 6 h, **stale** < 24 h
  (banner warning, last good data shown), otherwise **expired** (alerts paused, districts show "No data").
- `GET /api/health` reports per-source last success and failure.

## Field sensors (IoT)

```http
POST /api/sensors/readings
X-API-Key: <BLASTWATCH_INGEST_KEY>

{"station_id": "TNJ-01", "district": "Thanjavur",
 "readings": [{"ts": "2026-10-06T22:00:00+05:30", "temp_c": 24.8, "rh_pct": 97, "leaf_wet_min": 60}]}
```

Readings are floored to the hour. They override grid weather for that district and hour, and risk is
recomputed immediately. One station per district is the intended setup.

## Calibration

1. Collect presence **and absence** records from KVK / agriculture-department pest surveillance, TNAU
   reports or papers into a CSV shaped like [seed/observations_template.csv](blastwatch/seed/observations_template.csv)
   (`district,state,date,blast_present,severity,source,note`).
2. `observations import FILE`, then `backfill` the same period, then `backtest`.
3. The backtest prints, per threshold: hits, false alarms, misses, POD (outbreaks warned),
   FAR (warnings that were wrong), specificity and CSI. Pick a High threshold from that table,
   set it in `risk_rules.toml`, and bump `model_version`.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/outlook` | Officer view: action per district (alert / watch / none / unknown), 7-day levels, freshness |
| `GET /api/alerts`, `POST /api/alerts/{id}/sent` | Farmer messages and their status |
| `POST /api/sensors/readings` | Field sensor upload (API key) |
| `GET /api/health` | Freshness and per-source ingest status |
| `GET /api/risk?date=…`, `/api/districts/{id}/risk`, `/weather`, `/advice` | Map, series and advice |
| `GET /api/production`, `/api/genes`, `/api/genes/{symbol}` | FAOSTAT and GenBank data |

Interactive docs: `/docs`.

## Data notes

- **FAOSTAT:** the REST API requires authentication, so the public bulk CSV is used. Item 27 Rice;
  elements 5312 area, 5510 production, 5412 yield. 2024: India 217.9 Mt paddy vs mainland China 207.5 Mt.
- **GenBank:** queries use `[Title]`, because `[Gene]` returned unrelated records. Re-running `genes` replaces records.
- **District rice area** (`seed/districts.csv`) is blank until filled from the Tamil Nadu Season & Crop
  Report. Blank cells never erase a value stored in the database.
- **Varieties** list only entries with a cited source.

## Tests

```bash
python -m pytest
```

The tests cover the risk engine and wetness hierarchy, parsers, retries and provider fallback,
freshness, schema upgrade, alerts, calibration metrics and the API.

## Known gaps

1. No observation data yet, so the model is uncalibrated. This is the most important next step.
2. No login on the officer view or the "mark as sent" action.
3. No SMS/WhatsApp delivery or subscriber list yet.
4. Crop stage and variety are not known per field. A district-level alert cannot target susceptible fields.
