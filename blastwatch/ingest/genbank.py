"""GenBank records for blast resistance / avirulence genes via NCBI E-utilities."""
import time
from datetime import datetime

import httpx
from sqlalchemy import delete

from .. import config
from ..db import upsert
from ..models import Gene, GeneRecord
from ..runs import track

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _params(**extra) -> dict:
    params = {"db": "nuccore", "retmode": "json", "tool": "blastwatch", **extra}
    if config.NCBI_EMAIL:
        params["email"] = config.NCBI_EMAIL
    if config.NCBI_API_KEY:
        params["api_key"] = config.NCBI_API_KEY
    return params


def _pause() -> None:
    # Stay under NCBI's limit: 3 requests/s without a key, 10 with one.
    time.sleep(0.12 if config.NCBI_API_KEY else 0.35)


def parse_summary(payload: dict, gene_id: int) -> list[dict]:
    result = payload.get("result", {})
    rows = []
    for uid in result.get("uids", []):
        doc = result[uid]
        rows.append(
            {
                "gene_id": gene_id,
                "accession": doc.get("accessionversion") or doc.get("caption") or uid,
                "title": doc.get("title", ""),
                "length": doc.get("slen"),
                "organism": doc.get("organism"),
                "update_date": doc.get("updatedate"),
            }
        )
    return rows


def fetch_gene(client: httpx.Client, gene: Gene, retmax: int = 20) -> tuple[int, list[dict]]:
    search = client.get(f"{EUTILS}/esearch.fcgi", params=_params(term=gene.ncbi_query, retmax=retmax))
    search.raise_for_status()
    found = search.json()["esearchresult"]
    ids = found.get("idlist", [])
    _pause()
    if not ids:
        return int(found.get("count", 0)), []
    summary = client.get(f"{EUTILS}/esummary.fcgi", params=_params(id=",".join(ids)))
    summary.raise_for_status()
    _pause()
    return int(found.get("count", 0)), parse_summary(summary.json(), gene.id)


def ingest(session, retmax: int = 20) -> int:
    count = 0
    with httpx.Client(timeout=config.HTTP_TIMEOUT) as client, track(session, "genbank") as run:
        for gene in session.query(Gene).order_by(Gene.id):
            hits, rows = fetch_gene(client, gene, retmax)
            gene.ncbi_hits = hits
            gene.fetched_at = datetime.now()
            # Replace, so records from an older query don't linger after the query changes.
            session.execute(delete(GeneRecord).where(GeneRecord.gene_id == gene.id))
            count += upsert(session, GeneRecord, rows, ["gene_id", "accession"])
            session.commit()
        run.rows = count
    return count
