# Validation plan (order matters)

## A. Conversations: one extension officer and about ten farmers (this week; it cannot be done from a desk)

The open question is not the model. It is whether a message for a farmer's variety and crop stage leads to
a different action than the block bulletins farmers already get. Before the conversations, collect a few
recent TNAU AAS block advisories and Uzhavan messages for a Delta block, so you can compare like with like.

Book 30–45 minutes with an AAO or ADA in a Delta block. Ask these questions and write down the answers verbatim:

1. Would farmers give a sowing or transplanting date and variety to a WhatsApp/SMS service? Who would enter it?
2. Which channel do farmers actually read: WhatsApp, SMS, voice call, a village WhatsApp group, or the officer?
3. What do you already send (TNAU AAS block advisories, Uzhavan)? What is missing from it?
4. Have you seen blast outbreaks in the past 3 seasons? Where, when, on which varieties, and how was it confirmed?
5. Is there a record of this (PDI sheets, field-visit logs) that could be shared? Whom should we ask?
6. Would you verify farmer-reported symptoms? How many a week is realistic?
7. How do farmers tell blast from brown spot or sheath blight? What do they do when they first suspect it?
8. What are the local nursery age and duration of each common variety, and the blast rating of the top 5?
9. Who would sponsor or approve messages to farmers (department, TNAU or KVK)? This name replaces
   `<SPONSOR>` in the consent text.
10. Would an alert 2–3 days ahead change what a farmer does? What would that action be?

Then talk to about ten farmers (with the officer's introduction). Ask:

- What did you do the last time you suspected blast, and when did you notice it?
- Show a block advisory and a stage-specific draft message side by side. Which would you act on, and how?
- Where do you get these messages today: the officer, a village WhatsApp group, SMS, the dealer?
- Would you give your sowing or transplanting date and variety, and to whom?
- Would you change variety because of an alert? (Expect no. Variety follows the market price, MSP
  procurement, seed availability and grain preference, so the next-season advice is information, not a lever.)

**Stop rule:** if officers say farmers will not give a date or will not trust a message, or if farmers would
act the same on the block bulletin, change the design before writing more code.

**Channel for the pilot:** post into the officer's existing WhatsApp groups (copied from the outbox). That needs
no provider account and builds on trust the officer already has. The WhatsApp Business API needs business
verification, a provider, opt-in and per-message template fees. SMS needs DLT registration, and template
approval can take days to weeks. Check current prices before costing either.

Put the answers to question 8 into `blastwatch/seed/crop_calendar.csv`.

## B. Backtest against a baseline (`python -m blastwatch evaluate`)

1. Get observations from TRRI Aduthurai, TNAU, KVK or officer logs, with **absences as well as presences**.
   Record how each was confirmed in the `source` and `note` columns. Prefer natural-field records to
   induced hot-spot nurseries, and test with and without the nursery records.
   PDI time series from TNAU or TRRI are unlikely to be public, so this needs a data-sharing request.
2. `observations import FILE`, then `backfill` the same period (archived forecasts, the default).
3. Tune only on records before the holdout (`[split] holdout_start` in `config/eval_criteria.toml`,
   1 Oct 2026): `backtest --end 2026-09-30 --weather archive-forecast:d3`. The command refuses any later
   `--end`. Change `risk_rules.toml` from that table only.
4. Commit `config/eval_criteria.toml` and the rules, then run `evaluate --start 2026-10-15 --weather
   archive-forecast:d3` once, on held-out records only (the command refuses earlier dates). Report the
   verdict exactly as printed, including INCONCLUSIVE or FAIL. Never move `holdout_start` after seeing it.
5. If the verdict is FAIL or INCONCLUSIVE, do not tune `risk_rules.toml` on the same observations. Either
   collect more, or drop the weather model and keep the calendar rule plus crop-stage targeting.

Use archived forecasts (2024-01-22 onward) so the backtest sees forecast errors. Observations before that
can only be scored on reanalysis (`backfill --source power`, `--weather nasa-power`). Report those results
separately, as an upper bound. Outbreak records will be few, so keep the rule simple. A fitted model
(logistic regression, decision tree) on a few dozen events would overfit.

## C. Shadow pilot (one season, 2–3 blocks, ~30–50 farmers, officer-supervised)

- Enrol farmers through `POST /api/subscribers` after reading them the consent text.
- Farmer messages go to the outbox (`python -m blastwatch outbox`). The officer forwards them only after reviewing.
- Fungicide names and doses stay out of messages until TNAU reviews them. The Tamil texts need review by a
  Tamil-speaking agronomist first.
- Track:
  - alerts per block per week (alert fatigue)
  - the share of farmers who answer check-ins
  - officer-confirmed blast among alerted farmers vs the sampled non-alerted farmers
  - the action farmers report taking
- Agree what pilot success means with the officer before launch. A suggestion: the confirmed-blast rate among
  alerted farmers is clearly higher than among sampled non-alerted farmers, and more than 40 % of farmers answer.
