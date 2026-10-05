"""Runtime settings, read from environment variables or a local .env file."""
import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
SEED_DIR = PACKAGE_DIR / "seed"
WEB_DIR = ROOT / "web"
RULES_PATH = ROOT / "config" / "risk_rules.toml"


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
