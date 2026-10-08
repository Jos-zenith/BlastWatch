# BlastWatch

Rice blast early warning for field officers. It combines an **agricultural database**
(FAOSTAT), a **biological database** (GenBank) and **live weather** into one decision tool.
The pilot covers 18 rice-growing districts of Tamil Nadu, including all seven Cauvery delta districts.

## Who it is for

| User | What they get | Where |
|---|---|---|
| **Block / district agricultural officers, KVK staff** (primary) | Which districts need action now, a 7-day outlook, and a ready-made farmer message to review and forward | `/` (English / தமிழ், phone-friendly) |
| **Farmers** | Subscribed farmers get a short alert in Tamil or English, worded for their crop stage, and check-in questions afterwards. An officer forwards it from the outbox (or it goes by Telegram). Farmers never need the dashboard. | Messages |
| **Researchers / students** | Validation status, Rule A vs B comparison, FAOSTAT production trends, GenBank resistance genes, data-pipeline status | `/research` |
| **Anyone checking the forecast** | P(High) per night from 122 ensemble forecasts, and where weather centres disagree | `/confidence` |
| **Operators, demonstrations** | Live data flow, event feed, pipeline countdowns, virtual field station | `/live` |

The dashboard is a Vue 3 single-page app ([frontend/](frontend/)). It keeps one live connection
(Server-Sent Events) for all pages, so a forecast refresh, a sensor reading or a new alert reaches every
open page within a second, and moving between pages never reloads. The old `/*.html` links redirect.

Farmer messages never tell a farmer to spray on a forecast alone. Before heading they say **scout first,
spray only if spots are found**. At heading-flowering, when neck blast cannot be scouted in time, they
send the farmer to their agri officer about protecting the panicles. The model forecasts *weather
favourable for blast*, not outbreaks, and no fungicide name or dose appears until TNAU reviews it.


## Frontend

The built app (`frontend/dist/`) is committed, so `python -m blastwatch serve` serves it with no Node
toolchain on the server. To change it:

```bash
cd frontend
npm install
npm run dev     # http://localhost:5173, forwards /api to the FastAPI server on :8000
npm run build   # writes frontend/dist/, which FastAPI serves at /
```

`src/lib/live.js` holds the one event-stream connection, `src/lib/strings.js` the English and Tamil
text, and `src/views/` the four pages. Leaflet and Chart.js are bundled, not loaded from a CDN. Add
`?live=0` to any page for a static version (printing, report screenshots), and `?lang=ta` to open the
officer view in Tamil.

## Risk model (rules-v3)

Defined in [config/risk_rules.toml](config/risk_rules.toml). You can tune it without code changes.

- **Leaf wetness, first signal available per hour:** field sensor (wet ≥ 30 min) → Open-Meteo
  modelled leaf-wetness probability (≥ 70 %) → dew-point depression (T − Td ≤ 1.5 °C, or rain) → RH ≥ 93 %.
  Each day records which signal it used (`wetness_basis`), and the officer view shows it.
  Only the sensor is an independent measurement. The other three are humidity cutoffs on the same
  model fields: T − Td ≤ 1.5 °C is RH ≥ about 92 % at 28 °C, and the leaf-wetness probability is
  Open-Meteo's own derivation, not a measurement. Treat them as an RH threshold of about 93 % with a
  rain override, not as separate evidence.
- An hour is **conducive** when the leaf is wet and the temperature is 20–30 °C.
- Each day is scored over the **infection night** (12:00 → 12:00) so dew periods are not split at midnight.
- Score (0–100) = longest wet run (60, ramping from 8 h to 16 h) + total wet hours (20, from 8 h to 18 h)
  + light rain (10) + overcast (10), × crop susceptibility for the state and month.
  Levels: Low < 35 ≤ Moderate < 65 ≤ High. The officer view shows these points for every day (hover a day),
  so two days with equal wet hours but different levels can be explained.

### Why rules-v3: High has to be rare to mean anything

rules-v2 was red almost all season. On archived 1-day-ahead Open-Meteo forecasts for all districts,
22 Jan 2024 to 6 Oct 2026 (`backfill --lead-days 1`):

| | rules-v2 | rules-v3 |
|---|---|---|
| High, share of Oct–Jan district-days | 65 % (84 % in October) | 18 % (10–30 % by district) |
| Low, share of Oct–Jan district-days | 16 % | 63 % |
| Alert episodes per district per Oct–Jan season (with the 5-day cooldown) | median 16 | median 5 (1–11) |

In season the median night already had a 12-hour wet run on the v2 wetness rule, while v2 gave full run
points at 10 h, and the rain and cloud bonuses applied on about half of all days. Temperature sits in the
20–30 °C band almost every night, so it does not separate days either. v3 asks for wetter hours and gives no
run points until a run passes 8 h. **This tunes how often High occurs, not whether High predicts blast.**
That still needs observations (see Calibration). Re-run the comparison after any change to the rules.

### Limits, stated plainly

- **Not yet calibrated.** No outbreak data has been loaded. On history the model separates dry and
  monsoon seasons (March 2024 all Low, November 2023 almost all High) but cannot yet say which
  wet-season weeks lead to outbreaks. It scores weather, not crop stage, variety or inoculum.
- **Grid weather is not canopy weather.** Inside a flooded rice canopy, humidity is usually higher than at
  2 m, so a 2 m humidity cutoff is a proxy. Only leaf-wetness sensors can test it, and none are deployed.
- **Weather resolution is about 9 km.** For these districts, Open-Meteo's default model returns the same
  values and grid as ECMWF IFS (spacing about 0.07–0.08°, checked 7 Oct 2026). That is fine enough to
  separate blocks (see Block-level risk), but not canopy microclimate, which only a sensor sees.
- **Models disagree on leaf wetness.** For the same hour at Thanjavur, the leaf-wetness probability was
  43–87 % across ECMWF, GFS, ICON and JMA. Wetness near the threshold is uncertain.
- **Agreement between providers:** on 5 Oct 2026, Open-Meteo and MET Norway both rated Thanjavur High for
  6–7 Oct. Both rest on global model humidity, so this shows the rating is not tied to one provider. It is
  not validation.
- **High is set by frequency, not by outbreaks.** rules-v3 makes High uncommon (about 1 in 5 season days),
  which is what lets it carry information. Whether those days are the ones followed by blast is untested.
  If blast follows on far fewer days than that, most High days are still false alarms; the alert rule
  (2 of 3 days, 5-day cooldown) and crop-stage targeting limit how many messages that causes.

## Block-level risk: where in the district?

A district is scored at one point, its headquarters. Since v0.4 every **development block** (panchayat
union) in the 18 districts is also scored, at its headquarters town, with the same rules
([blastwatch/blocks.py](blastwatch/blocks.py)).

**Where the points come from** ([scripts/build_block_seed.py](scripts/build_block_seed.py), output
[blastwatch/seed/blocks.csv](blastwatch/seed/blocks.csv)). The block list is Wikidata's "community
development block of Tamil Nadu" (187 blocks in these districts). Each block is placed at its
headquarters town, found among OpenStreetMap place nodes **inside that block's district boundary**, so a
same-named village elsewhere cannot be picked. Tamil names are matched first, then English names after
normalising transliteration. Nominatim and Wikidata's own coordinates are fallbacks, accepted only when
the point is verified to lie in the right district. Roads and lakes named after a town are rejected.
Every row records how it was located (`located_by`). Documented alternative spellings
(`ALIASES` in the script, e.g. Rajasingamangalam → R.S. Mangalam) are tried the same way. 174 blocks were
located. The 13 left out are listed by the script, not guessed. Three are Chennai suburbs, but some are
rice blocks (Thiruvonam, Uppiliyapuram, and Annagramam, Kammapuram, Keerapalayam and Nallur in Cuddalore).
Adding one needs a verified headquarters coordinate or a spelling that OpenStreetMap knows. The build also caught two Wikidata errors and
handles them: Keelaiyur and Kilvelur have their Tamil names swapped (swapped back, because farmer
messages use them), and Ammapettai is listed in two districts (kept where it is actually found).
Polygon centroids would be better than headquarters points, but no open block-boundary layer for Tamil
Nadu was found (OpenStreetMap's level-6 boundaries are taluks, not blocks).

**Does it add information?** Measured on the forecast of 8 Oct 2026, with the first 168 blocks:

- The 168 block points fall in **166 distinct forecast grid cells**; only 14 share their district
  headquarters' cell. So blocks get their own weather, not copies of the district's.
- Over the next 3 nights, a block's level differed from its district headquarters' on **22 %** of
  block-nights, and half the district-nights (27 of 54) had blocks at two or more levels. Thanjavur on
  9 Oct: headquarters Moderate, 5 of 13 blocks High. Ramanathapuram on 10 Oct: headquarters Low, 4 of 9
  blocks High.
- This is one forecast, not a season. Neighbouring cells are correlated and a level threshold turns small
  differences into different colours, so part of the spread is grid noise. A season of archived
  forecasts at block points would measure it properly.

**What uses it:**

- Officer view: each day shows "n/m blocks" rated High; the detail panel lists the blocks riskiest first
  ("where to scout first": next 3 days, 3-day maximum level, longest wet run, enrolled farmers) and draws
  them on the map. Selecting a block shows a farmer message worded for that block, e.g.
  "மதுரை (வாடிப்பட்டி வட்டாரம்): …", but only when the block itself meets the alert rule on fresh data.
- Farmer alerts: a farmer whose enrolment names a known block (spelling variants and Tamil names are
  matched) is alerted on **that block's** risk, and the message names the block. One point per farmer,
  so the alert rate per farmer does not change. While block data is stale, farmers fall back to the
  district.
- `GET /api/blocks?date=`, `GET /api/districts/{id}/blocks`, and `blocks` counts in `/api/outlook`.

**The district alert rule is configurable (Rule A / Rule B).** By default it still uses the headquarters
point (Rule A, `[alerts] district_rule = "hq"`). Setting `district_rule = "blocks_half"` switches to
Rule B: a district day is High when at least `block_share` of its scored blocks are High, falling back to
Rule A while block data is stale. The officer view and `/api/outlook` always report both rules' actions,
and `python -m blastwatch compare-rules` or the Research page compares them over stored days and the
coming forecast week. Alerting when
*any* block is High would double how often a district is High (12 → 24 of 54 district-nights in
the same forecast), because each of ~10 blocks gets its own chance. That would undo the base-rate tuning of
rules-v3. A "half the blocks High" rule gave 12 of 54, the same rate as the headquarters, and agreed with it
on 11 of those 12. It is a candidate replacement that is less sensitive to one point. Switch only after
comparing the two rules over a season.

**Cost.** One Open-Meteo call per block per 3-hourly refresh: about 1,400 a day, plus about 140 for
districts and about 880 for the ensemble, well under the free 10,000. The ensemble stays per district
(per block it would cost about 2,000 calls per run). Blocks use Open-Meteo only. If it fails, district
risk and alerts carry on and the officer view marks block data stale.

## Ensemble risk: how sure is a High night?

The officer view rates each night from one deterministic forecast. Leaf wetness is where weather models
disagree most, so BlastWatch also scores **every member** of three global ensembles from Open-Meteo's
Ensemble API ([blastwatch/ensemble.py](blastwatch/ensemble.py)):

| Ensemble | Members | |
|---|---|---|
| ECMWF IFS ENS 0.25° | 51 | the same centre as the live forecast |
| NCEP GEFS 0.25° | 31 | US |
| DWD ICON EPS | 40 | Germany |

Each member is scored with exactly the same rules as the live forecast (`score_series`), so a member is
one plausible version of the coming nights. Per district and night the system stores P(High), P(Moderate)
and the 10th, 50th and 90th percentile scores, per ensemble and pooled. The pool weights the three
centres **equally**, not by member count, so ECMWF's 51 members cannot outvote the other two.

What it showed on its first run (8 Oct 2026): for Madurai, 84 % and 92 % of ECMWF members scored 9–10 Oct
High, while GEFS and ICON gave 0–3 %. Pooled, that is about 30 %. The rule-based forecast (ECMWF-based)
rated both nights High and raised an alert, and the ensemble shows that the alert rests on one centre.
On the same run, 15 district-nights had one centre at ≥ 50 % and another below 10 %.

- `/ensemble.html` shows a P(High) heatmap (districts × nights, with the rule-based level marked), the
  score spread for a district, and each centre's P(High) side by side, with a table view.
- The officer view shows P(High) under each day and in the day tooltip.
- An experimental **ensemble action** (≥ 2 of the next 3 nights with P(High) ≥ 50 %, `[ensemble] alert_p`)
  is reported next to the rule-based action for comparison. **Alerts still follow the rule-based
  forecast.**
- Refreshed every 6 h by the scheduler (`[ensemble] refresh_hours`), or `python -m blastwatch ensemble`.
- **Cost.** Open-Meteo does not document how ensemble requests are counted. Its docs price a one-location,
  one-variable ICON EPS (40-member) request at 4.0 calls, which fits members ÷ 10. On that assumption one
  refresh of all three ensembles for 18 districts costs about 220 calls, 880 a day at 6-hourly refreshes,
  and requests are paced to stay under the per-minute limit.

**Limits.** P(High) is a probability of *High-risk weather under the current rules*, not of blast.
Whether 70 % nights are followed by blast more often than 30 % nights needs observations (Calibration).
The ensembles run on coarser grids (roughly 25–40 km) than the live forecast (about 9 km), and each
centre derives leaf wetness differently, so a persistent gap between centres is partly model bias, not
only weather uncertainty. Scoring past ensemble runs against outbreaks would separate the two.

## Live updates and the virtual field station

`GET /api/stream` is a Server-Sent Events stream ([blastwatch/events.py](blastwatch/events.py)). The
scheduler, the sensor endpoint and the virtual station publish to an in-process bus:

| Event | When |
|---|---|
| `refresh.started`, `refresh.weather`, `refresh.done` | each step of a forecast refresh |
| `risk.changed` / `risk.unchanged` | after rescoring, with every district-day whose level changed and why |
| `alerts.evaluated` | alerts created, updated or withdrawn |
| `sensor.reading` | each field-sensor upload, with the rescored day |
| `ensemble.*` | each ensemble model fetched, failed or stored |
| `sim.*` | the virtual station started, finished, failed or was reset |

The last 200 events are kept, so a page that opens shows recent activity (`GET /api/events`), and a
browser that reconnects sends `Last-Event-ID` and receives what it missed. A sensor upload now rescores
only its own district, so a reading reaches every dashboard within a second.

**Virtual field station** (`/live.html`, needs `BLASTWATCH_DEMO=1`). It replays last night's infection
window (yesterday 12:00 to now) for one district, one simulated hour per step, through the same ingest as a
real sensor. Scenarios: a long dew night (scores High), evening showers with a dry break (Moderate), and a
dry night (Low). The page shows each reading arrive, the score climb, the district recolour on the map,
and the alert rule run at the end. **Reset** deletes every simulated reading (station ids starting `SIM`),
rescores and withdraws any alert they caused. Run it once before a demonstration; at 1.5 s per hour a full
night takes about 35 s.

To show the hardware path instead, run the server with `BLASTWATCH_INGEST_KEY` set and, in a second
terminal, `python -m blastwatch station --district Thanjavur --scenario dew`. It posts JSON to
`POST /api/sensors/readings` exactly as an ESP32 with a leaf-wetness sensor would.

The bus lives in one process. Run one server process (as the Procfile does); several workers would each
have their own bus.

## Alerts

An alert is created only when **≥ 2 of the next 3 days are High** (forecast skill drops after ~3 days),
the forecast is **fresh**, and the district has had no alert in the last 5 days. Alerts are stored as
`ready` for an officer to review, copy (Tamil / English) and mark as sent. Nothing is sent
automatically yet; wiring an SMS/WhatsApp provider is a deliberate next step.

- **Messages never go stale.** Each refresh rewrites a `ready` message from the same risk rows the
  7-day strip shows. Dates are written with weekday names ("Thu 9 Oct, Fri 10 Oct" / "வியாழன் 9/10"), and
  the view shows when the text was generated. When a district stops meeting the rule, its unsent message
  is withdrawn (`expired`) and cannot be marked as sent. If the risk returns within the cooldown, it is
  reissued. A sent message is never rewritten.
- **The district message is for the officer's WhatsApp group.** Enrolled farmers get their own message for
  their variety and crop stage (see below), and the view shows how many enrolled farmers are at a
  susceptible stage.
- **Field checks close the loop.** After scouting, the officer taps *Blast found* or *No blast found*
  (`POST /api/districts/{id}/field-checks`, optionally linked to the alert). Each check is stored and
  becomes an observation for calibration. Routine visits without an alert count too, so outbreaks the
  model missed are recorded and not only its alarms.

The Tamil text (UI and messages) was drafted for this prototype. **A Tamil-speaking agronomist
should review it** (e.g. TNAU or KVK extension staff) before it reaches farmers.

## Farmer subscriptions

An officer or operator enrols a farmer after reading them the consent text (`GET /api/consent`, Tamil and
English). The enrolment records the district, an optional block for field visits, the variety, and the
sowing date (direct-seeded) or transplanting date. Risk stays per district: blocks share weather grid cells.

**Crop stage** is counted back from maturity, using the variety duration in
[seed/crop_calendar.csv](blastwatch/seed/crop_calendar.csv) and `[crop_stage]` in `risk_rules.toml`. Rice's
reproductive phase (~35 days) and ripening (~30 days) are nearly fixed; only the vegetative phase changes with
duration. Transplanted crops add the nursery age (21 days by default, flagged). A variety not in the calendar
gets stage `unknown`: the farmer is still alerted, with advice that covers every stage.

**Each refresh, 07:00–20:00 only,** a subscriber gets an alert when:
- their district meets the alert rule above,
- the forecast has not expired,
- their crop is vegetative, booting, heading or unknown (not ripening or harvested),
- and they had no delivered alert in the last 5 days. A failed Telegram send doesn't count, so it is retried.

**Check-ins** go out 7 and 14 days after an alert, because lesions show days after infection. A sample of
non-alerted farmers in susceptible stages (15 % a week) is asked too, so missed outbreaks show up. A farmer
gets one open question at a time. Replies come in through `POST /api/replies`.

**A YES is only a report.** An officer checks the field and verifies it (`POST /api/reports/{id}/verify`).
The verdict becomes an observation for calibration: present if confirmed, absent if rejected (e.g. brown spot).

## Data freshness and reliability

- Pages read only the local database. Weather APIs are called by the background scheduler, never on a
  page request.
- Requests retry on timeouts, 429 and 5xx with exponential backoff (honouring `Retry-After`).
- If Open-Meteo fails, MET Norway is used (about 2.5 days of hourly data, dew-point wetness).
- Open-Meteo is fetched in multi-location requests of up to 100 districts. Batching saves round
  trips, not quota: Open-Meteo bills every location as at least one call, so requests after the first
  are paced to stay under 500 calls/min (the free limit is 600). The 18 pilot districts need one request.
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
2. `observations import FILE`, then `backfill` the same period, then
   `backtest --weather archive-forecast:d3`. Officer-verified farmer reports are added as observations
   automatically.
   - **Score on archived forecasts, not reanalysis.** The live system acts on forecasts, which are
     wrong more often than reanalysis. `backfill` therefore defaults to Open-Meteo's archived runs at a
     fixed lead (`--lead-days 3`, the far edge of the alert horizon). Their humidity starts on
     2024-01-22, so 2024 onward covers about three Samba seasons. The ECMWF IFS archive alone starts
     2025-10-03. For older outbreaks use `--source power` (NASA POWER reanalysis) and report that result
     as an upper bound.
   - Pin one weather source with `--weather` on both `backtest` and `evaluate`. Without it, hours are merged
     across sources and a result cannot be tied to one data type.
3. The backtest prints, per threshold: hits, false alarms, misses, POD (outbreaks warned),
   FAR (warnings that were wrong), specificity and CSI. Pick a High threshold from that table,
   set it in `risk_rules.toml`, and bump `model_version`.
4. For a go/no-go decision, run `evaluate` once on observations the thresholds were **not** tuned on.
   It compares the model with a calendar + humidity baseline, using criteria committed in advance in
   [config/eval_criteria.toml](config/eval_criteria.toml), and prints PASS, FAIL or INCONCLUSIVE.
   It requires at least 30 presences, 30 absences and 3 districts. It prints the hashes of the criteria
   and rules files, so a result can be tied to the exact settings that produced it.

See [docs/FIELD_VALIDATION.md](docs/FIELD_VALIDATION.md) for the order of validation steps.

**Public records were audited and are not enough.** To backtest before any field data existed, every
monthly TNAU Pest and Disease Surveillance and Forecast report from January 2024 to September 2026 was
read (33 reports; [docs/tnau_surveillance_audit.csv](docs/tnau_surveillance_audit.csv) lists each one with
its rice blast statement, verbatim). 24 report no rice blast observation, 6 say only that blast was
"observed in major rice growing districts of Tamil Nadu", 2 name districts outside the pilot, and **one**
names a pilot district (Tiruvarur, June 2024, by month, and together with brown spot and sheath rot). That
cannot support precision or recall. A presence-only backtest would also mislead: with High on about 1 night
in 5, a 12-day lead window would contain a High night about 90 % of the time if nights were independent,
so "most outbreaks were preceded by a High night" proves little. This is why every field check and every
verified farmer report is stored as a labelled observation; the Research page shows progress towards the
pre-registered minimums.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/outlook` | Officer view: action per district (alert / watch / none / unknown), 7-day levels, freshness, subscriber and report counts |
| `GET /api/alerts`, `POST /api/alerts/{id}/sent` | District messages, when each was generated, its field checks and status (`ready` / `sent` / `expired`) |
| `POST /api/districts/{id}/field-checks`, `GET …/field-checks` | Record a field visit (blast found or not), which becomes a calibration observation; list recent ones |
| `POST /api/sensors/readings` | Field sensor upload (API key) |
| `GET /api/consent` | Consent text (Tamil / English) to read before enrolment |
| `POST /api/subscribers`, `GET /api/subscribers`, `POST /api/subscribers/stop` | Enrol or update a farmer, list with crop stage, unsubscribe (officer key) |
| `GET /api/farmer-messages`, `POST /api/farmer-messages/{id}/sent` | The outbox, and marking a message forwarded (officer key) |
| `POST /api/replies` | A farmer's YES / NO / NOT SURE answer (officer key) |
| `GET /api/reports`, `POST /api/reports/{id}/verify` | Reports waiting for a field check, and the officer's verdict (officer key) |
| `GET /api/health` | Freshness and per-source ingest status |
| `GET /api/stream` | Server-Sent Events: refreshes, sensor readings, risk changes, alerts as they happen |
| `GET /api/events?since=…` | Recent events (the last 200 are kept) |
| `GET /api/live/status` | Connected pages, next scheduled runs, freshness, virtual station state |
| `POST /api/refresh`, `POST /api/ensemble/refresh` | Run a refresh now (demo mode) |
| `GET /api/sim`, `POST /api/sim/start`, `/stop`, `/reset` | Virtual field station (demo mode) |
| `GET /api/blocks?date=` | Every block's level and score on a date, for the map |
| `GET /api/districts/{id}/blocks` | Each block of a district over the next 7 days, riskiest first |
| `GET /api/blocks/{id}/message` | Farmer message for one block's group, when that block meets the alert rule |
| `GET /api/rules/compare?days=` | Rule A vs Rule B over stored days and the forecast week |
| `GET /api/validation` | Labelled observations vs the pre-registered minimums, and the surveillance audit |
| `GET /api/ensemble` | P(High) per district and night, per ensemble and pooled, next to the rule-based level |
| `GET /api/risk?date=…`, `/api/districts/{id}/risk`, `/weather`, `/advice` | Map, series and advice |
| `GET /api/production`, `/api/genes`, `/api/genes/{symbol}` | FAOSTAT and GenBank data |

Interactive docs: `/docs`.

## Data notes

- **FAOSTAT:** the REST API requires authentication, so the public bulk CSV is used. Item 27 Rice;
  elements 5312 area, 5510 production, 5412 yield. 2024: India 217.9 Mt paddy vs mainland China 207.5 Mt.
- **GenBank:** queries use `[Title]`, because `[Gene]` returned unrelated records. Re-running `genes` replaces records.
- **No district or block rice area.** Production-at-risk was dropped, and nothing else needs area, so the
  area column was removed. Block-level seasonal paddy area is not reliably published, and many public block
  shapefiles predate the post-2019 district splits (e.g. Mayiladuthurai).
- **Varieties** list only entries with a cited source.

## Tests

```bash
python -m pytest
```

The tests cover the risk engine and wetness hierarchy, parsers, retries and provider fallback,
freshness, schema upgrade, alerts, crop stage, farmer alerts and check-ins, report verification,
calibration and evaluation metrics, the API, the event bus, ensemble parsing, scoring and pooling (with a
failing model), sensor uploads that rescore and publish, the simulation scenarios, the virtual station
run and reset, the block seed file, block ingest, scoring and rollup, farmer alerts from their own block,
district Rule A / Rule B with fallback, block messages, the rule comparison and validation endpoints, and
serving the built frontend (client-side routes, bundles, no path traversal).

## Known gaps

1. No observation data yet, so the model is uncalibrated. This is the most important next step.
2. No login on the officer view, the district "mark as sent" action or field checks. Anyone who can reach
   the server can add a field check, which feeds calibration data. Put the dashboard behind a login before
   it is shared outside the pilot team. The farmer endpoints use a shared officer key, not per-officer accounts.
3. No WhatsApp or SMS adapter, and no inbound chat webhook. Farmer messages wait in the outbox for an
   officer to forward, or go by Telegram, which has not been tested against real chats.
4. The dashboard shows enrolled-farmer counts by stage, but enrolment, the farmer outbox and report
   verification still run through the API and CLI only.
7. Block points are headquarters towns, not polygon centroids, and a few blocks are missing (see
   Block-level risk). Field sensors still post per district, not per block. The district alert rule
   still uses one point; the "half the blocks" rule needs a season-long comparison first.
8. Seed advice was removed from the officer view. It listed a breeding line and rested on gene presence. It
   returns only when TNAU field ratings by season are loaded for released varieties.
5. `seed/crop_calendar.csv` durations come from the project background document and are unverified, and
   nursery ages are blank. Stage boundaries are a rule of thumb. Fill both from the TNAU Crop Production Guide.
6. The consent text has a `<SPONSOR>` placeholder. All Tamil farmer texts, including the consent, need review
   by a Tamil-speaking agronomist before enrolment starts.
