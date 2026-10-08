# BlastWatch pilot (dev build v0.1)

Weather-based rice-blast early warning for a handful of Cauvery Delta blocks, with
variety/stage personalisation, consented enrolment, and a feedback loop verified by extension officers.

**Status: runs end to end, tests pass, live Open-Meteo call verified. Nothing in the risk rule is validated.**
Every threshold in `config/params.yaml` is a hypothesis. Do not send alerts to real farmers until
`docs/FIELD_VALIDATION.md` step A (extension-officer conversation) is done and chemical advice is reviewed.

## Design choices (and what each replaces)

| Choice | Instead of | Why |
|---|---|---|
| SQLite + one daily cron job | PostGIS, Celery, microservices | A 6-block pilot needs none of it; one file, one command |
| Open-Meteo (no key) | TAWN / NASA POWER ingestion | Works today. TAWN has no public API I could rely on; add it once access is confirmed |
| Rule: N favourable hours/day on M of next 3 days | Weighted risk index with invented coefficients | Explainable to an officer, every number lives in one YAML |
| Variety rating field, `unknown` by default | GenBank R-gene matching | Gene presence does not predict field resistance to local races; ratings come from TNAU/TRRI |
| Days after sowing / transplanting (+ per-variety nursery age) | Growing degree days | Simpler; direct-seeded has its own branch |
| Susceptible stage gate, 3-day look-ahead, 5-day cooldown | Always alert | Limits alert fatigue |
| No production-at-risk number | PaR formula | It could exceed block production and ignored weather |
| Follow-ups at +7 and +14 days, plus a sample of **non-alerted** farmers | 48 h feedback after alerts only | Lesions lag infection; missed outbreaks otherwise invisible |
| Farmer "yes" is `unverified` until an officer confirms | Auto-confirm | Blast is confused with brown spot / sheath blight |
| `outbox` channel (operator forwards on WhatsApp/SMS) + Telegram adapter | Twilio/WhatsApp API on day one | No account/approval blockers to start the pilot |
| `eval/criteria.yaml` with baseline and pass/fail fixed in advance | Retrofitted success | Model must beat a calendar+humidity rule, else it adds nothing |

## Run it

```bash
pip install -r requirements.txt
set PYTHONPATH=src            # PowerShell: $env:PYTHONPATH="src"
python -m pytest              # 8 tests, offline
python -m blastwatch run      # fetch -> risk -> alerts -> follow-ups (schedule daily)
python -m blastwatch outbox   # messages waiting to be forwarded
python -m blastwatch serve    # API on :8000 (docs at /docs)
```

Env: `BLASTWATCH_DB` (db path), `BLASTWATCH_OFFICER_KEY` (required for officer endpoints),
`TELEGRAM_BOT_TOKEN` (only for the telegram channel).

API: `GET /consent`, `POST /enrol` (consent required; transplanted or direct_seeded),
`POST /stop/{contact}`, `POST /reply`, `GET /status`, officer-only `GET /officer/reports`, `POST /officer/verify`.

## Backtest

```bash
python eval/evaluate.py --labels eval/labels.csv   # see eval/labels_template.csv
```
Reports average precision, precision and recall against a calendar baseline, with a block-month
cluster bootstrap. Fewer than 30 events returns INCONCLUSIVE by construction. Commit
`criteria.yaml` before running it. Uses reanalysis, not archived forecasts, so it flatters real-time skill.

## Known gaps (do not paper over)

- **Over-alerting risk:** on its first live run (2026-10-05) the default rule rated 3 of 6 blocks HIGH.
  Likely too sensitive; the backtest or officer feedback must set the thresholds.
- Block coordinates are approximate town locations (`config/blocks.csv`); neighbouring blocks share a
  weather grid cell, so weather is not the differentiator, variety/stage timing is. Unproven that farmers want that.
- `config/varieties.yaml` durations/ratings are from a background doc, unverified; nursery ages are empty.
- `config/chemicals.yaml` is `reviewed: false`, so fungicide advice is withheld by design. The Tamil advisory
  text is a draft that needs a native-speaker and TNAU review.
- Consent text names no sponsor yet (`<SPONSOR - to be named>`).
- No labelled outbreak data is included; whether usable data exists (and is not just induced-nursery PDI) is unknown.
- Replies come in via `POST /reply`; there is no inbound Telegram/WhatsApp webhook yet.
- Leaf wetness is approximated from gridded RH/rain, not measured in the canopy.
