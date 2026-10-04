"""Command line: python -m blastwatch <command>."""
import argparse
import logging
from datetime import date

from .db import SessionLocal, init_db
from .ingest import faostat, genbank, weather
from .pipeline import compute_risk, load_seeds


def cmd_init(args):
    init_db()
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


def cmd_all(args):
    cmd_init(args)
    cmd_faostat(argparse.Namespace(region="Asia", refresh=False))
    cmd_genes(argparse.Namespace(retmax=20))
    cmd_weather(argparse.Namespace(past_days=3, forecast_days=7))
    cmd_risk(args)


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
    sub.add_parser("all", help="init + faostat + genes + weather + risk").set_defaults(func=cmd_all)

    sv = sub.add_parser("serve", help="run the API and dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--with-scheduler", action="store_true", help="refresh weather+risk every 3 h")
    sv.set_defaults(func=cmd_serve)

    sub.add_parser("schedule", help="run the refresh loop on its own").set_defaults(func=cmd_schedule)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
