"""Command line: python -m blastwatch <command>."""
import argparse
import logging
from datetime import date
from pathlib import Path

from . import alerts, calibration
from .db import SessionLocal, init_db
from .ingest import faostat, genbank, weather
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
        print("historical hours upserted:", weather.ingest_backfill(s, args.start, args.end))


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
        r = calibration.backtest(s, load_rules(), args.start, args.end, args.lead)
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


def cmd_all(args):
    cmd_init(args)
    cmd_faostat(argparse.Namespace(region="Asia", refresh=False))
    cmd_genes(argparse.Namespace(retmax=20))
    cmd_refresh(args)


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

    b = sub.add_parser("backfill", help="fetch historical hourly weather from NASA POWER")
    b.add_argument("--start", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    b.add_argument("--end", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    b.set_defaults(func=cmd_backfill)

    f = sub.add_parser("faostat", help="load FAOSTAT rice production from the bulk download")
    f.add_argument("--region", default="Asia", help="bulk file region, e.g. Asia, Africa, Americas")
    f.add_argument("--refresh", action="store_true", help="re-download even if cached")
    f.set_defaults(func=cmd_faostat)

    g = sub.add_parser("genes", help="fetch GenBank records for the seeded genes")
    g.add_argument("--retmax", type=int, default=20)
    g.set_defaults(func=cmd_genes)

    sub.add_parser("risk", help="recompute daily blast risk").set_defaults(func=cmd_risk)
    sub.add_parser("alerts", help="raise farmer alerts for sustained high risk").set_defaults(func=cmd_alerts)
    sub.add_parser("refresh", help="weather (with fallback) + risk + alerts").set_defaults(func=cmd_refresh)
    sub.add_parser("all", help="init + faostat + genes + refresh").set_defaults(func=cmd_all)

    o = sub.add_parser("observations", help="field observations for calibration")
    o_sub = o.add_subparsers(dest="action", required=True)
    oi = o_sub.add_parser("import", help="import a CSV shaped like seed/observations_template.csv")
    oi.add_argument("file")
    oi.set_defaults(func=cmd_observations)

    bt = sub.add_parser("backtest", help="score the model against imported observations")
    bt.add_argument("--start", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    bt.add_argument("--end", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    bt.add_argument("--lead", type=int, default=3, help="days of warning that count as a hit")
    bt.set_defaults(func=cmd_backtest)

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
