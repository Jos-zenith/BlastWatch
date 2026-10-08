"""Runtime settings, read from environment variables or a local .env file."""
import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
SEED_DIR = PACKAGE_DIR / "seed"
# The built Vue app (frontend/: npm run build).
WEB_DIR = ROOT / "frontend" / "dist"
RULES_PATH = ROOT / "config" / "risk_rules.toml"
EVAL_CRITERIA_PATH = ROOT / "config" / "eval_criteria.toml"


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()

DATABASE_URL = os.getenv(
    "BLASTWATCH_DATABASE_URL", f"sqlite:///{(DATA_DIR / 'blastwatch.db').as_posix()}"
)
# NCBI asks every E-utilities client to identify itself; an API key raises the limit to 10 req/s.
NCBI_EMAIL = os.getenv("NCBI_EMAIL", "")
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "")
# All weather timestamps are stored as naive local time in this zone.
TIMEZONE = "Asia/Kolkata"
HTTP_TIMEOUT = float(os.getenv("BLASTWATCH_HTTP_TIMEOUT", "60"))
# Shared secret for POST /api/sensors/readings; the endpoint is disabled while unset.
INGEST_KEY = os.getenv("BLASTWATCH_INGEST_KEY", "")
# MET Norway's terms require an identifying User-Agent with contact details.
USER_AGENT = os.getenv("BLASTWATCH_USER_AGENT", "blastwatch/0.2 (rice blast early warning; student project)")
# Shared secret for officer endpoints (enrolment, replies, report verification, outbox).
# Those endpoints are disabled while it is unset.
OFFICER_KEY = os.getenv("BLASTWATCH_OFFICER_KEY", "")
# Only needed for subscribers on the "telegram" channel.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Demo controls (virtual field station, "refresh now"): they change what officers see, so they are
# off unless BLASTWATCH_DEMO=1.
DEMO = os.getenv("BLASTWATCH_DEMO", "").lower() in ("1", "true", "yes")
