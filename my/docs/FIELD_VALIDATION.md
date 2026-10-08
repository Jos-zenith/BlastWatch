# Validation plan (order matters)

## A. Extension-officer conversation (this week; the one step that cannot be done from a desk)
Book 30-45 min with an AAO/ADA in a Delta block. Ask, and write down verbatim answers:
1. Would farmers give a sowing/transplanting date and variety to a WhatsApp/SMS service? Who would enter it?
2. Which channel do farmers actually read: WhatsApp, SMS, voice call, village WhatsApp group, officer?
3. What do you already send (TNAU AAS block advisories, Uzhavan)? What is missing from them?
4. Have you seen blast outbreaks in the past 3 seasons? Where, when, which varieties, how confirmed?
5. Is there any record of this (PDI sheets, field-visit logs) that could be shared, and with whom to ask?
6. Would you verify farmer-reported symptoms? How many per week is realistic?
7. How do farmers tell blast from brown spot / sheath blight? What do they do on first suspicion?
8. Nursery age and duration by variety for local practice; blast rating of the top 5 varieties.
9. Who would sponsor/approve messages to farmers (department, TNAU, KVK)?
10. Would an alert 2-3 days ahead change what a farmer does, and what would that action be?

Stop rule: if officers say farmers will not enter a date or will not trust a message, change the design before writing more code.

## B. Backtest with a baseline (`eval/evaluate.py`)
1. Obtain labels (TRRI Aduthurai / TNAU / KVK / officer logs). Record a `definition` per row. Prefer natural-field
   records over induced hot-spot nurseries; flag the latter and test with and without them.
2. Commit `eval/criteria.yaml`. Run once. Report the verdict as printed, including INCONCLUSIVE or FAIL.
3. If FAIL or INCONCLUSIVE: do not tune thresholds on the same labels; collect more or drop the weather model and
   ship the calendar rule plus stage/variety targeting.

## C. Shadow pilot (one season, 2-3 blocks, ~30-50 farmers, officer-supervised)
- Run `python -m blastwatch run` daily; messages go to the `outbox` and the officer forwards only after reviewing.
- Chemical advice stays withheld until `chemicals.yaml` is TNAU-reviewed.
- Track: alerts per block per week (alert fatigue), % of farmers answering follow-ups, officer-confirmed blast
  among alerted vs non-alerted samples, farmer-reported action taken.
- Pilot success is defined before launch with the officer (suggested: confirmed-blast rate in alerted farmers is
  clearly higher than in the sampled non-alerted farmers, and response rate > 40%).
