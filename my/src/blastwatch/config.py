import csv
import hashlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CFG = ROOT / "config"


def load_params(path: Path | None = None) -> dict:
    return yaml.safe_load((path or CFG / "params.yaml").read_text(encoding="utf-8"))


def params_hash(path: Path | None = None) -> str:
    return hashlib.sha256((path or CFG / "params.yaml").read_bytes()).hexdigest()[:12]


def load_blocks() -> list[dict]:
    with open(CFG / "blocks.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["lat"], r["lon"] = float(r["lat"]), float(r["lon"])
    return rows


def load_varieties() -> dict:
    return yaml.safe_load((CFG / "varieties.yaml").read_text(encoding="utf-8"))["varieties"]


def load_chemicals() -> dict:
    return yaml.safe_load((CFG / "chemicals.yaml").read_text(encoding="utf-8"))
