"""Build blastwatch/seed/blocks.csv: development blocks in the pilot districts, each located at its
headquarters town. Run once and commit the CSV; re-run only to refresh it.

    python scripts/build_block_seed.py            # writes blastwatch/seed/blocks.csv
    python scripts/build_block_seed.py --report   # also prints every match for review

Sources, in order:
1. Block list: Wikidata items of class "community development block of Tamil Nadu" (Q123009250),
   with their district and Tamil label. Wikidata coordinates are NOT used: only about a third of the
   blocks have one, and some are copied between neighbouring blocks.
2. Location: the block's headquarters town (a block is named after it, e.g. Thiruvaiyaru block ->
   Thiruvaiyaru), found among OpenStreetMap place nodes INSIDE that block's district boundary. A name
   that matches a place in another district therefore cannot be picked. Tamil names are matched first
   (exact), then English names after normalising transliteration (th/t, dh/d, doubled letters).
3. Fallback: Nominatim search "<name>, <district>, Tamil Nadu", accepted only when Nominatim's address
   places the result in the same district, and only for features that sit in a town (a place, office,
   clinic, station, administrative area). Roads, rivers and lakes are rejected: a road named after a
   town can run 100 km from it.
4. Last fallback: the block's own Wikidata coordinate, accepted only when reverse geocoding puts it in
   the same district and no other block has the same point (some are copied between blocks).
Blocks that none of these locate are left out and listed, never guessed.

Overpass downloads are cached in data/raw/osm/ (delete the folder to refresh).

A headquarters point is the same approximation the districts already use; the forecast grid
(~9-11 km) is the real limit on spatial detail.
"""
import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "blastwatch" / "seed"
CACHE = ROOT / "data" / "raw" / "osm"
UA = {"User-Agent": "BlastWatchStudentProject/0.3 (https://github.com/; rice blast early warning, student project)"}
WIKIDATA = "https://query.wikidata.org/sparql"
OVERPASS = "https://overpass-api.de/api/interpreter"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
# Nominatim feature classes that can lie far from the town they are named after.
REJECT_CLASSES = {"highway", "waterway", "natural", "landuse", "leisure", "water"}
PLACE_RANK = {"city": 0, "town": 1, "suburb": 2, "village": 3, "hamlet": 4}
TA_BLOCK_SUFFIX = "ஊராட்சி ஒன்றியம்"
# Other spellings of a block's headquarters, tried after its Wikidata name. Each is still matched only
# among places inside the block's district (or via Nominatim with a district check), never trusted alone.
ALIASES = {
    "Talanayar": ["Thalainayar"],
    "Tattayyangarpettai": ["Thathaiyangarpet"],
    "Tirunavalur": ["Thirunavalur"],
    "Kunnattur": ["Kundrathur"],
    "Rajasingamangalam": ["R.S. Mangalam"],
    "Thoockanaickenpalaiyam": ["T.N. Palayam", "Thuckanaivkampalayam"],  # the second is OSM's spelling
}
TA_DISTRICT_SUFFIX = "மாவட்டம்"


def wikidata_blocks(client: httpx.Client) -> list[dict]:
    query = """SELECT ?item ?en ?ta ?dta ?coord WHERE {
      ?item wdt:P31 wd:Q123009250 ; wdt:P131 ?d . ?d wdt:P131 wd:Q1445 .
      ?item rdfs:label ?en FILTER(LANG(?en) = "en")
      OPTIONAL { ?item rdfs:label ?ta FILTER(LANG(?ta) = "ta") }
      OPTIONAL { ?item wdt:P625 ?coord }
      ?d rdfs:label ?dta FILTER(LANG(?dta) = "ta")
    }"""
    r = client.post(WIKIDATA, data={"query": query}, headers={**UA, "Accept": "application/sparql-results+json"})
    r.raise_for_status()
    blocks = {}
    for b in r.json()["results"]["bindings"]:
        qid = b["item"]["value"].rsplit("/", 1)[1]
        district_ta = b["dta"]["value"].replace(TA_DISTRICT_SUFFIX, "").strip()
        # One entry per (item, district): an item can be (wrongly) in two districts on Wikidata, and
        # main() keeps the district where it is actually found.
        blocks.setdefault((qid, district_ta), {
            "wikidata": qid,
            "name": re.sub(r"\s+block$", "", b["en"]["value"], flags=re.I).strip(),
            "name_ta": (b.get("ta", {}).get("value") or "").replace(TA_BLOCK_SUFFIX, "").strip() or None,
            "district_ta": district_ta,
            "coord": _point(b.get("coord", {}).get("value")),
        })
    counts = {}
    for qid, _ in blocks:
        counts[qid] = counts.get(qid, 0) + 1
    for (qid, _), block in blocks.items():
        block["multi_district"] = counts[qid] > 1
    return list(blocks.values())


def _point(wkt: str | None) -> tuple[float, float] | None:
    """Wikidata "Point(lon lat)" -> (lat, lon)."""
    m = re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", wkt or "")
    return (float(m.group(2)), float(m.group(1))) if m else None


def overpass(client: httpx.Client, query: str) -> list[dict]:
    for attempt in range(4):
        r = client.post(OVERPASS, data={"data": query}, headers=UA)
        if r.status_code in (429, 504) and attempt < 3:
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()["elements"]
    raise RuntimeError("overpass unavailable")


def district_relations(client: httpx.Client) -> dict[str, int]:
    """{Tamil district name: OSM relation id}."""
    elements = overpass(client, """[out:json][timeout:120];
        area["name"="Tamil Nadu"]["admin_level"="4"]->.tn;
        relation(area.tn)["boundary"="administrative"]["admin_level"="5"]; out tags;""")
    return {e["tags"]["name:ta"].replace(TA_DISTRICT_SUFFIX, "").strip(): e["id"]
            for e in elements if "name:ta" in e["tags"]}


def places_in(client: httpx.Client, relation_id: int) -> list[dict]:
    cache = CACHE / f"places_{relation_id}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    elements = overpass(client, f"""[out:json][timeout:120];
        rel({relation_id});map_to_area->.d;
        node(area.d)["place"~"^(city|town|suburb|village|hamlet)$"]["name"]; out body;""")
    places = [{"lat": e["lat"], "lon": e["lon"], **e["tags"]} for e in elements]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(places, ensure_ascii=False), encoding="utf-8")
    time.sleep(2)
    return places


def norm_en(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z]", "", s)
    # Common spelling variants of the same Tamil word: koil/kovil, pet/pettai, palayam/palaiyam.
    s = s.replace("kovil", "koil").replace("pettai", "pet").replace("palaiyam", "palayam")
    for a, b in (("zh", "l"), ("th", "t"), ("dh", "d"), ("kh", "k"), ("ph", "p"), ("bh", "b"),
                 ("w", "v"), ("ee", "i"), ("oo", "u"), ("aa", "a"), ("y", "")):
        s = s.replace(a, b)
    s = re.sub(r"(.)\1+", r"\1", s)  # doubled letters
    return s.replace("d", "t").replace("g", "k")  # voiced/unvoiced pairs are one letter in Tamil


def norm_ta(name: str | None) -> str | None:
    """Tamil vowel signs can be stored composed or decomposed; compare in NFC without spaces."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", name)) if name else None


def best(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    return min(candidates, key=lambda p: (PLACE_RANK.get(p.get("place"), 9), -int(p.get("population", "0") or 0)))


def match(block: dict, places: list[dict]) -> tuple[dict | None, str | None]:
    """Tamil name first (exact), then English. When both match but different places, the English item
    label wins and the Tamil label is remembered as pointing elsewhere (see fix_swapped_labels).
    A block with no Tamil label takes its town's."""
    ta = norm_ta(block["name_ta"])
    ta_hit = best([p for p in places if ta in (norm_ta(p.get("name:ta")), norm_ta(p.get("name")))]) if ta else None
    en = norm_en(block["name"])
    en_hit = best([p for p in places if en in {norm_en(p.get(k, "")) for k in ("name", "name:en", "alt_name", "old_name")} - {""}])
    if ta_hit and en_hit and ta_hit is not en_hit:
        block["ta_points_to"] = (round(ta_hit["lat"], 4), round(ta_hit["lon"], 4))
        hit, method = en_hit, "osm-en"
    elif ta_hit:
        hit, method = ta_hit, "osm-ta"
    else:
        hit, method = en_hit, "osm-en"
    if hit and not block["name_ta"] and hit.get("name:ta"):
        block["name_ta"] = hit["name:ta"]
        block["note"] = "Tamil name from OSM (none on Wikidata)"
    return (hit, method) if hit else (None, None)


def fix_swapped_labels(rows: list[dict], report: bool) -> None:
    """Block A's Tamil label naming block B's town is a mislabel. When B's label is different, the two
    were swapped on Wikidata (Keelaiyur / Kilvelur): swap them back. Otherwise A's label is cleared."""
    by_point = {(r["district"], r["lat"], r["lon"]): r for r in rows}
    for a in rows:
        target = a.pop("_ta_points_to", None)
        if not target:
            continue
        b = by_point.get((a["district"], *target))
        if b is not None and b is not a and norm_ta(b["name_ta"]) != norm_ta(a["name_ta"]):
            a["name_ta"], b["name_ta"] = b["name_ta"], a["name_ta"]
            note = f"Tamil labels of {a['name']} and {b['name']} were swapped on Wikidata; swapped back"
        else:
            a["name_ta"] = ""
            note = f"Tamil label of {a['name']} names another place; cleared"
        if report:
            print("  fix:", note)
    for r in rows:
        r.pop("_ta_points_to", None)


def nominatim(client: httpx.Client, block: dict, district_en: str) -> dict | None:
    time.sleep(1.1)  # Nominatim usage policy: at most one request per second
    r = client.get(NOMINATIM, headers=UA, params={
        "q": f"{block['name']}, {district_en}, Tamil Nadu", "format": "jsonv2", "addressdetails": 1,
        "countrycodes": "in", "limit": 3})
    r.raise_for_status()
    for hit in r.json():
        if hit.get("category") in REJECT_CLASSES:
            continue
        if in_district(hit.get("address", {}), district_en):
            return {"lat": float(hit["lat"]), "lon": float(hit["lon"]), "name": hit.get("name"),
                    "place": f"{hit.get('category')}/{hit.get('type')}"}
    return None


def in_district(address: dict, district_en: str) -> bool:
    return norm_en(district_en) in norm_en(" ".join(str(v) for v in address.values()))


def wikidata_point(client: httpx.Client, block: dict, district_en: str, taken: set) -> dict | None:
    if block["coord"] is None or block["coord"] in taken:
        return None
    time.sleep(1.1)
    lat, lon = block["coord"]
    r = client.get(NOMINATIM_REVERSE, headers=UA, params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10})
    r.raise_for_status()
    if not in_district(r.json().get("address", {}), district_en):
        return None
    return {"lat": lat, "lon": lon, "name": block["name"], "place": "wikidata point"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--report", action="store_true", help="print every block and what it matched")
    args = parser.parse_args()
    with open(SEED / "districts.csv", encoding="utf-8") as f:
        districts = {d["name_ta"]: d["name"] for d in csv.DictReader(f)}

    rows, missing = [], []
    with httpx.Client(timeout=180) as client:
        all_blocks = wikidata_blocks(client)
        coords = [b["coord"] for b in all_blocks if b["coord"]]
        shared = {c for c in coords if coords.count(c) > 1}
        blocks = [b for b in all_blocks if b["district_ta"] in districts]
        relations = district_relations(client)
        for district_ta, district_en in districts.items():
            places = places_in(client, relations[district_ta])
            for block in sorted((b for b in blocks if b["district_ta"] == district_ta), key=lambda b: b["name"]):
                hit, method = match(block, places)
                for alias in ALIASES.get(block["name"], []) if hit is None else []:
                    hit, method = match({**block, "name": alias, "name_ta": None}, places)
                    if hit is None:
                        hit, method = nominatim(client, {**block, "name": alias}, district_en), "nominatim"
                    if hit is not None:
                        break
                if hit is None:
                    hit, method = nominatim(client, block, district_en), "nominatim"
                if hit is None:
                    hit, method = wikidata_point(client, block, district_en, shared), "wikidata"
                if hit is None:
                    missing.append(f"{block['name']} ({district_en}, {block['wikidata']})")
                    continue
                rows.append({"district": district_en, "name": block["name"], "name_ta": block["name_ta"] or "",
                             "lat": round(float(hit["lat"]), 4), "lon": round(float(hit["lon"]), 4),
                             "located_by": method, "hq_place": hit.get("name", ""), "wikidata": block["wikidata"],
                             "_ta_points_to": block.get("ta_points_to"), "_multi": block["multi_district"]})
                if args.report:
                    note = f"  ({block['note']})" if block.get("note") else ""
                    print(f"{district_en:16} {block['name']:24} -> {hit.get('name')} [{hit.get('place')}] via {method}{note}")

    # An item listed in two districts keeps the one placement that does not land on another block's
    # town; if none or both survive, it is reported instead of guessed.
    single_points = {(r["lat"], r["lon"]) for r in rows if not r["_multi"]}
    for r in [r for r in rows if r["_multi"] and (r["lat"], r["lon"]) in single_points]:
        rows.remove(r)
    for qid in {r["wikidata"] for r in rows if r["_multi"]}:
        placed = [r for r in rows if r["wikidata"] == qid]
        if len(placed) > 1:
            for r in placed:
                rows.remove(r)
            missing.append(f"{placed[0]['name']} ({qid}): on Wikidata in {len(placed)} districts and found in each")
    for r in rows:
        r.pop("_multi")

    # Two blocks on one point means one of them matched the other's town: keep a block whose own
    # English name matches the place, drop the rest as conflicts.
    by_point: dict[tuple, list[dict]] = {}
    for r in rows:
        by_point.setdefault((r["lat"], r["lon"]), []).append(r)
    for group in (g for g in by_point.values() if len(g) > 1):
        own = [r for r in group if norm_en(r["name"]) == norm_en(r["hq_place"])]
        for r in group:
            if r not in own[:1]:
                rows.remove(r)
                missing.append(f"{r['name']} ({r['district']}, {r['wikidata']}): matched {r['hq_place']}, "
                               f"which belongs to another block")

    fix_swapped_labels(rows, args.report)
    rows.sort(key=lambda r: (r["district"], r["name"]))
    with open(SEED / "blocks.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} blocks; not located: {len(missing)}")
    for m in missing:
        print("  missing:", m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
