# BlastWatch

Rice blast early-warning and resistant-variety advisor. It combines an **agricultural database**
(FAOSTAT), a **biological database** (GenBank) and **live weather** into one decision tool.
The pilot covers 17 rice-growing districts of Tamil Nadu.

| Question | Data |
|---|---|
| Where is blast risk high in the next 5 days? | Open-Meteo hourly forecast → rule-based risk engine |
| How much rice is at stake? | FAOSTAT crop production (rice, item 27) + district rice area |
| Which resistant varieties / genes fit? | GenBank (NCBI E-utilities) + curated variety list |

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt

python -m blastwatch all          # create DB, load seeds, FAOSTAT, GenBank, forecast, risk
python -m blastwatch serve        # http://127.0.0.1:8000
```

`serve --with-scheduler` refreshes the forecast and risk every 3 hours. `python -m blastwatch schedule`
runs the refresh loop on its own.

### Commands

| Command | What it does |
|---|---|
| `init` | Create tables and load `blastwatch/seed/*.csv` |
| `faostat [--region Asia] [--refresh]` | Load rice production from the FAOSTAT bulk download (cached in `data/raw/`) |
| `genes [--retmax 20]` | Fetch GenBank records for every seeded gene |
| `weather [--past-days 3] [--forecast-days 7]` | Fetch the Open-Meteo hourly forecast for all districts (one batched request) |
| `backfill --start YYYY-MM-DD --end YYYY-MM-DD` | Historical hourly weather from NASA POWER (for backtesting) |
| `risk` | Recompute daily risk from all stored weather |
| `serve` / `schedule` | API + dashboard / refresh loop |

### Configuration

Copy `.env.example` to `.env`:

- `NCBI_EMAIL`, `NCBI_API_KEY` identify you to NCBI. A free key raises the limit from 3 to 10 requests/s.
- `BLASTWATCH_DATABASE_URL` defaults to SQLite at `data/blastwatch.db`. Set a
  PostgreSQL URL for deployment; both `postgresql://…` and `postgresql+psycopg://…` are supported.
  The Psycopg 3 binary driver is installed from `requirements.txt`. The upsert helper supports SQLite and PostgreSQL.

## Risk model (rules-v1)

Defined in [config/risk_rules.toml](config/risk_rules.toml). You can tune it without code changes.

- An hour is **conducive** when RH ≥ 90 % and 20–30 °C (a proxy for leaf wetness).
- Each day is scored over the **infection night**, 12:00 the day before to 12:00 on that day, so dew
  periods are not split at midnight.
- Score (0–100) = longest unbroken conducive run (60) + total conducive hours (20) + light rain
  0.5–20 mm (10) + overcast ≥ 70 % (10), × crop susceptibility for the state and month.
- Levels: Low < 35 ≤ Moderate < 65 ≤ High.

**Sanity check (NASA POWER history, Thanjavur and Madurai):** March 2024 (dry) scored Low on 30/30 days.
November 2023 (northeast monsoon) scored High on 26–27 of 29–30 days. The model separates seasons.
It does not yet discriminate well *within* the wet season. That is the job of the calibration
step against recorded outbreaks (plan weeks 4–5).

## API

| Endpoint | Returns |
|---|---|
| `GET /api/health` | Counts, last weather fetch, last risk run |
| `GET /api/districts` | Pilot districts |
| `GET /api/risk?date=YYYY-MM-DD` | All districts for a date, ranked, with production exposed |
| `GET /api/districts/{id}/risk` | Daily series (past week + forecast) |
| `GET /api/districts/{id}/weather` | Hourly series |
| `GET /api/districts/{id}/advice` | Worst level over the next 5 days + varieties for the state |
| `GET /api/production?areas=India&areas=Bangladesh&start=1990` | FAOSTAT rice series |
| `GET /api/genes`, `GET /api/genes/{symbol}` | Genes and cached GenBank records |

Interactive docs: `/docs`.

## Data notes

- **FAOSTAT:** the REST API now requires authentication, so the project uses the public bulk CSV
  (`bulks-faostat.fao.org`). Codes: item 27 Rice; elements 5312 area (ha), 5510 production (t),
  5412 yield (kg/ha). 2024 data: India 217.9 Mt paddy vs mainland China 207.5 Mt.
- **GenBank:** queries use the `[Title]` field. `[Gene]` returned unrelated records (e.g. MADS-box
  for "Pi2"). Re-running `genes` replaces each gene's records.
- **District rice area** (`seed/districts.csv`, `rice_area_ha`) is intentionally blank. Fill it from
  the Tamil Nadu Season & Crop Report to enable the "production exposed" figure.
- **Varieties** (`seed/varieties.csv`) list only entries with a cited source. Extend the list with TNAU/ICAR
  releases.

## Tests

```bash
python -m pytest
```

These tests cover the risk engine, all parsers (offline fixtures) and the API on a temporary database.

## Next steps (from the dev plan)

1. Fill in district rice area; extend the curated variety list.
2. Backtest: `backfill` 3–5 seasons, compare against recorded outbreaks, tune `risk_rules.toml`.
3. Alerts (email/Telegram) when a district turns High; a field-report form for ground truth.
4. Phase 4: ML risk model trained on field reports; image-based leaf diagnosis.
