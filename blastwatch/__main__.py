"""Command line: python -m blastwatch <command>."""
import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

from . import alerts, calibration, config, farmers
from sqlalchemy import select

from .db import SessionLocal, init_db
from .ingest import faostat, genbank, weather
from .models import FarmerMessage, Subscriber
from .pipeline import compute_risk, load_seeds
from .risk import load_rules


def cmd_init(args):
    added = init_db()
    if added:
        print("added columns:", ", ".join(added))
    with SessionLocal() as s:
        print("seeded", load_seeds(s))


def cmd_weather(args):
    with SessionLocal() as s:
        print("weather hours upserted:", weather.ingest_forecast(s, args.past_days, args.forecast_days))


def cmd_backfill(args):
    with SessionLocal() as s:
        print("historical hours upserted:",
              weather.ingest_backfill(s, args.start, args.end, args.source, args.lead_days))


def cmd_faostat(args):
    with SessionLocal() as s:
        print("FAOSTAT area-years upserted:", faostat.ingest(s, args.region, args.refresh))


def cmd_genes(args):
    with SessionLocal() as s:
        print("GenBank records upserted:", genbank.ingest(s, args.retmax))


def cmd_risk(args):
    with SessionLocal() as s:
        print("risk days computed:", compute_risk(s))


def cmd_alerts(args):
    with SessionLocal() as s:
        print(alerts.evaluate(s, load_rules()))


def cmd_farmers(args):
    with SessionLocal() as s:
        print(farmers.notify_farmers(s, load_rules()))


def cmd_outbox(args):
    with SessionLocal() as s:
        rows = s.execute(
            select(FarmerMessage, Subscriber).join(Subscriber, Subscriber.id == FarmerMessage.subscriber_id)
            .where(FarmerMessage.status == "queued").order_by(FarmerMessage.created_at)
        ).all()
    if not rows:
        print("outbox empty")
    for m, sub in rows:
        print(f"--- #{m.id} to {sub.contact} [{m.kind}{', ' + m.stage if m.stage else ''}] {m.created_at:%Y-%m-%d %H:%M}")
        print(m.body + "\n")


def cmd_refresh(args):
    from .scheduler import refresh

    print(refresh())


def cmd_observations(args):
    with SessionLocal() as s:
        print("observations upserted:", calibration.import_observations(s, Path(args.file)))


def _pct(v):
    return "   -" if v is None else f"{v * 100:4.0f}%"


def cmd_backtest(args):
    with SessionLocal() as s:
        r = calibration.backtest(s, load_rules(), args.start, args.end, args.lead,
                                 weather_source=args.weather)
    print(f"observations: {r['observations']} ({r['present']} with blast, {r['absent']} without); "
          f"skipped for missing weather: {r['skipped_no_weather']}")
    if not r["observations"]:
        print("No observations in range. Import some with `observations import FILE` first.")
        return
    if not r["absent"]:
        print("WARNING: no absence records, so false alarms cannot be measured.")
    print(f"warned = any day scored >= threshold within {args.lead} days before the observation\n")
    print("threshold   TP  FP  FN  TN    POD   FAR  Spec.   CSI")
    for x in r["results"]:
        mark = "  <- current High" if x.threshold == r["current_high"] else ""
        print(f"{x.threshold:9.0f} {x.tp:4} {x.fp:3} {x.fn:3} {x.tn:3}  {_pct(x.pod)} {_pct(x.far)} "
              f"{_pct(x.specificity)} {_pct(x.csi)}{mark}")
    print("\nPOD = outbreaks warned; FAR = warnings that were false alarms; CSI balances both.")


def _num(v):
    return "-" if v is None else f"{v:.2f}"


def cmd_evaluate(args):
    crit = calibration.load_criteria()
    rules = load_rules()
    print(f"criteria {calibration.file_hash(config.EVAL_CRITERIA_PATH)} | "
          f"rules {calibration.file_hash(config.RULES_PATH)} ({rules['model_version']}) | "
          f"weather {args.weather or 'merged (all sources)'}")
    with SessionLocal() as s:
        rows = calibration.evaluation_table(s, rules, crit, args.start, args.end, args.weather)
    if not rows:
        print("No observations with weather in their lead window. Import observations and backfill first.")
        return
    v = calibration.verdict(rows, crit)
    print(f"observations: {v['present']} with blast, {v['absent']} without, "
          f"{v['districts_with_present']} districts with blast")
    print(f"average precision  model {_num(v['model_ap'])}  baseline {_num(v['baseline_ap'])}")
    print(f"at alert threshold model precision {_num(v['model_precision'])} recall {_num(v['model_recall'])} | "
          f"baseline precision {_num(v['baseline_precision'])} recall {_num(v['baseline_recall'])}")
    if v["ap_gain_ci95"]:
        lo, hi = v["ap_gain_ci95"]
        print(f"AP gain 95% CI: {lo:+.2f} to {hi:+.2f}")
    print("VERDICT:", v["verdict"])


def cmd_ensemble(args):
    from . import ensemble

    with SessionLocal() as s:
        print(ensemble.ingest(s, load_rules(), args.models))


def cmd_station(args):
    """A field station over HTTP, as a real one (e.g. an ESP32 with a leaf-wetness sensor) would post.
    Replays last night's window for one scenario, one reading per `interval` seconds."""
    import random
    import time
    from datetime import datetime

    import httpx

    from . import sim

    rng = random.Random(args.seed)
    headers = {"X-API-Key": args.key or config.INGEST_KEY}
    with httpx.Client(base_url=args.url, timeout=30) as client:
        for ts in sim.replay_hours(datetime.now()):
            r = sim.reading(args.scenario, ts, rng)
            body = {"station_id": args.station, "district": args.district,
                    "readings": [{"ts": ts.isoformat(), **r}]}
            resp = client.post("/api/sensors/readings", json=body, headers=headers)
            resp.raise_for_status()
            day = resp.json().get("day") or {}
            print(f"{ts:%a %H:%M}  {r['temp_c']:>5} °C  RH {r['rh_pct']:>5}  wet {r['leaf_wet_min']:>2} min  "
                  f"-> {day.get('date')} {day.get('level')} {day.get('score')}")
            time.sleep(args.interval)


def cmd_compare_rules(args):
    from . import blocks

    with SessionLocal() as s:
        r = blocks.compare_rules(s, load_rules(), args.start, args.end)
    print(f"Rule A = district headquarters, Rule B = >= {r['block_share']:.0%} of blocks High, {r['start']} to {r['end']}")
    print(f"{'district':16} {'days':>5} {'A High':>7} {'B High':>7} {'both':>5} {'same level':>11}")
    for d in r["districts"] + [{"district": "ALL", **r["total"]}]:
        same = f"{d['agree'] / d['days']:.0%}" if d["days"] else "-"
        print(f"{d['district']:16} {d['days']:>5} {d['hq_high']:>7} {d['blocks_high']:>7} {d['both_high']:>5} {same:>11}")


def cmd_all(args):
    cmd_init(args)
    cmd_faostat(argparse.Namespace(region="Asia", refresh=False))
    cmd_genes(argparse.Namespace(retmax=20))
    cmd_refresh(args)
    cmd_ensemble(argparse.Namespace(models=None))


def cmd_serve(args):
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(scheduler=args.with_scheduler), host=args.host, port=args.port)


def cmd_schedule(args):
    from .scheduler import run_forever

    run_forever()


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="blastwatch", description="Rice blast early-warning system")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create tables and load seed CSVs").set_defaults(func=cmd_init)

    w = sub.add_parser("weather", help="fetch Open-Meteo forecast for all districts")
    w.add_argument("--past-days", type=int, default=3)
    w.add_argument("--forecast-days", type=int, default=7)
    w.set_defaults(func=cmd_weather)

    b = sub.add_parser("backfill", help="historical hourly weather: archived forecasts or NASA POWER reanalysis")
    b.add_argument("--start", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    b.add_argument("--end", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    b.add_argument("--source", choices=["forecast", "power"], default="forecast",
                   help="forecast: Open-Meteo archived runs (humidity from 2024-01-22); power: NASA reanalysis")
    b.add_argument("--lead-days", type=int, default=3,
                   help="use the run this many days before each hour (1-7); 3 = edge of the alert horizon")
    b.set_defaults(func=cmd_backfill)
    weather_help = "score only this weather source, e.g. archive-forecast:d3 or nasa-power (default: merged)"

    f = sub.add_parser("faostat", help="load FAOSTAT rice production from the bulk download")
    f.add_argument("--region", default="Asia", help="bulk file region, e.g. Asia, Africa, Americas")
    f.add_argument("--refresh", action="store_true", help="re-download even if cached")
    f.set_defaults(func=cmd_faostat)

    g = sub.add_parser("genes", help="fetch GenBank records for the seeded genes")
    g.add_argument("--retmax", type=int, default=20)
    g.set_defaults(func=cmd_genes)

    sub.add_parser("risk", help="recompute daily blast risk").set_defaults(func=cmd_risk)
    sub.add_parser("alerts", help="district alerts for officers to review and forward").set_defaults(func=cmd_alerts)
    sub.add_parser("refresh", help="weather (with fallback) + risk + district alerts + farmer messages").set_defaults(func=cmd_refresh)
    sub.add_parser("all", help="init + faostat + genes + refresh + ensemble").set_defaults(func=cmd_all)

    en = sub.add_parser("ensemble", help="score every member of the ECMWF / GEFS / ICON ensembles: P(High) per night")
    en.add_argument("--models", nargs="+", choices=["ecmwf", "gefs", "icon"], help="default: [ensemble] models")
    en.set_defaults(func=cmd_ensemble)

    cr = sub.add_parser("compare-rules", help="district alert Rule A (headquarters) vs Rule B (half the blocks)")
    cr.add_argument("--start", type=date.fromisoformat, default=date.today() - timedelta(days=30))
    cr.add_argument("--end", type=date.fromisoformat, default=date.today())
    cr.set_defaults(func=cmd_compare_rules)

    st = sub.add_parser("station", help="field station client: post simulated readings to a running server")
    st.add_argument("--district", required=True)
    st.add_argument("--scenario", choices=["dew", "showers", "dry"], default="dew")
    st.add_argument("--station", default="SIMCLI", help="station id; ids starting with SIM are removed by the live page's reset")
    st.add_argument("--url", default="http://127.0.0.1:8000")
    st.add_argument("--key", help="default: BLASTWATCH_INGEST_KEY")
    st.add_argument("--interval", type=float, default=1.0, help="seconds between readings")
    st.add_argument("--seed", type=int)
    st.set_defaults(func=cmd_station)
    sub.add_parser("farmers", help="farmer alerts and due check-ins (send hours only)").set_defaults(func=cmd_farmers)
    sub.add_parser("outbox", help="farmer messages waiting for an officer to forward").set_defaults(func=cmd_outbox)

    o = sub.add_parser("observations", help="field observations for calibration")
    o_sub = o.add_subparsers(dest="action", required=True)
    oi = o_sub.add_parser("import", help="import a CSV shaped like seed/observations_template.csv")
    oi.add_argument("file")
    oi.set_defaults(func=cmd_observations)

    bt = sub.add_parser("backtest", help="score the model against imported observations")
    bt.add_argument("--start", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    bt.add_argument("--end", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    bt.add_argument("--lead", type=int, default=3, help="days of warning that count as a hit")
    bt.add_argument("--weather", help=weather_help)
    bt.set_defaults(func=cmd_backtest)

    ev = sub.add_parser("evaluate", help="pre-registered verdict: model vs baseline (config/eval_criteria.toml)")
    ev.add_argument("--start", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    ev.add_argument("--end", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    ev.add_argument("--weather", help=weather_help)
    ev.set_defaults(func=cmd_evaluate)

    sv = sub.add_parser("serve", help="run the API and dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--with-scheduler", action="store_true",
                    help="bootstrap reference data, then refresh weather/risk/alerts every 3 h")
    sv.set_defaults(func=cmd_serve)

    sub.add_parser("schedule", help="run the refresh loop on its own").set_defaults(func=cmd_schedule)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
