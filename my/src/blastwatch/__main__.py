"""CLI:  python -m blastwatch run | serve | outbox"""
import sys

from .db import connect


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "help"
    if cmd == "run":
        from .pipeline import run_daily
        print(run_daily(connect()))
    elif cmd == "serve":
        import uvicorn
        uvicorn.run("blastwatch.api:app", host="127.0.0.1", port=8000)
    elif cmd == "outbox":
        for r in connect().execute(
                "SELECT m.id, s.contact, m.kind, m.sent_at, m.body FROM messages m"
                " JOIN subscribers s ON s.id=m.subscriber_id WHERE m.status='outbox' ORDER BY m.id DESC LIMIT 50"):
            print(f"--- #{r['id']} to {r['contact']} [{r['kind']}] {r['sent_at']}\n{r['body']}\n")
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
