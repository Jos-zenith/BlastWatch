"""FAOSTAT crop production (dataset QCL) from the bulk CSV download.

The FAOSTAT REST API now requires authentication, so we use the public bulk files.
Codes: item 27 = Rice (paddy); elements 5312 = area harvested (ha), 5510 = production (t),
5412 = yield (kg/ha).
"""
import csv
import io
import zipfile
from pathlib import Path

import httpx

from .. import config
from ..db import upsert
from ..models import Production
from ..runs import track

BULK_URL = "https://bulks-faostat.fao.org/production/Production_Crops_Livestock_E_{region}.zip"
RICE_ITEM_CODE = 27
ELEMENT_FIELDS = {"5312": "area_ha", "5510": "production_t", "5412": "yield_kg_ha"}


def parse_rows(text_stream, item_code: int = RICE_ITEM_CODE) -> list[dict]:
    """Turn the wide FAOSTAT CSV (one Y<year> column per year) into one row per area-year."""
    reader = csv.DictReader(text_stream)
    merged: dict[tuple[int, int], dict] = {}
    for row in reader:
        if int(row["Item Code"]) != item_code or row["Element Code"] not in ELEMENT_FIELDS:
            continue
        field = ELEMENT_FIELDS[row["Element Code"]]
        area_code = int(row["Area Code"])
        for col, value in row.items():
            if not (col.startswith("Y") and col[1:].isdigit()) or value in ("", None):
                continue
            year = int(col[1:])
            rec = merged.setdefault(
                (area_code, year),
                {
                    "area_code": area_code,
                    "area": row["Area"],
                    "item_code": item_code,
                    "item": row["Item"],
                    "year": year,
                    "area_ha": None,
                    "production_t": None,
                    "yield_kg_ha": None,
                    "source": "faostat",
                },
            )
            rec[field] = float(value)
    return list(merged.values())


def download(region: str = "Asia", dest_dir: Path = config.RAW_DIR, refresh: bool = False) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"faostat_qcl_{region}.zip"
    if refresh or not path.exists():
        with httpx.stream("GET", BULK_URL.format(region=region), timeout=300) as resp:
            resp.raise_for_status()
            with open(path, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    return path


def read_zip(path: Path, item_code: int = RICE_ITEM_CODE) -> list[dict]:
    with zipfile.ZipFile(path) as z:
        name = next(
            n for n in z.namelist() if n.endswith(".csv") and n.split("_")[-1] not in
            ("AreaCodes.csv", "Elements.csv", "Flags.csv", "ItemCodes.csv", "NOFLAG.csv")
        )
        with z.open(name) as raw:
            return parse_rows(io.TextIOWrapper(raw, encoding="latin-1"), item_code)


def ingest(session, region: str = "Asia", refresh: bool = False) -> int:
    with track(session, "faostat") as run:
        rows = read_zip(download(region, refresh=refresh))
        run.rows = upsert(session, Production, rows, ["area_code", "item_code", "year", "source"])
    return run.rows
