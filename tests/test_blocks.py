"""Block-level risk: ingest, scoring, rollup, API, and farmer alerts from the farmer's own block."""
import csv
from datetime import date, datetime, time, timedelta

import httpx
from sqlalchemy import select

from blastwatch import blocks, farmers
from blastwatch.db import upsert
from blastwatch.models import Block, BlockRiskDaily, BlockWeatherHourly, District, FarmerMessage, IngestRun, Subscriber
from blastwatch.risk import load_rules

from .conftest import synthetic_weather

RULES = load_rules()
MORNING = datetime.combine(date.today(), time(10))


def add_block(s, district: str, name: str, wet: bool, name_ta: str | None = None, weather: bool = True) -> Block:
    d = s.scalar(select(District).where(District.name == district))
    b = Block(district_id=d.id, name=name, name_ta=name_ta, lat=d.lat + 0.1, lon=d.lon + 0.1, located_by="test")
    s.add(b)
    s.flush()
    if weather:
        rows = [{**{k: v for k, v in r.items() if k != "district_id"}, "block_id": b.id, "source": blocks.SOURCE}
                for r in synthetic_weather(b.id, wet)]
        upsert(s, BlockWeatherHourly, rows, blocks.KEYS)
    s.commit()
    return b


def mark_block_run(s) -> None:
    s.add(IngestRun(source=blocks.SOURCE, started_at=datetime.now(), finished_at=datetime.now(), status="ok", rows=1))
    s.commit()


def test_seed_file_is_well_formed():
    """Every pilot district has blocks, every point sits near its district, and provenance is recorded."""
    path = blocks.config.SEED_DIR / "blocks.csv"
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    districts = {d["name"]: d for d in csv.DictReader(open(blocks.config.SEED_DIR / "districts.csv", encoding="utf-8"))}
    assert len(rows) >= 150 and {r["district"] for r in rows} == set(districts)
    assert len({(r["district"], r["name"]) for r in rows}) == len(rows)
    assert len({(r["lat"], r["lon"]) for r in rows}) == len(rows)  # no copied coordinates
    for r in rows:
        d = districts[r["district"]]
        # A block HQ lies within ~90 km of its district HQ (Tamil Nadu districts are < 150 km across).
        assert abs(float(r["lat"]) - float(d["lat"])) < 0.8 and abs(float(r["lon"]) - float(d["lon"])) < 0.8, r
        assert r["located_by"] in {"osm-ta", "osm-en", "nominatim", "wikidata"} and r["wikidata"].startswith("Q")


def test_ingest_keys_rows_by_block(Session):
    with Session() as s:
        b = add_block(s, "Thanjavur", "Test block", wet=False, weather=False)
        n_blocks = len(s.scalars(select(Block)).all())

        def handler(request: httpx.Request) -> httpx.Response:
            lats = request.url.params["latitude"].split(",")
            hourly = {"time": ["2026-10-08T00:00", "2026-10-08T01:00"], "temperature_2m": [25, 24],
                      "relative_humidity_2m": [95, 96], "dew_point_2m": [24, 23], "precipitation": [0, 0],
                      "cloud_cover": [80, 90], "leaf_wetness_probability": [80, 85]}
            return httpx.Response(200, json=[{"hourly": hourly}] * len(lats))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            assert blocks.ingest_forecast(s, client=client) == 2 * n_blocks
        rows = s.scalars(select(BlockWeatherHourly).where(BlockWeatherHourly.block_id == b.id)).all()
        assert len(rows) == 2 and {r.source for r in rows} == {blocks.SOURCE}
        assert blocks.fresh(s, RULES)


def test_rollup_and_api_show_where_in_the_district_risk_sits(client, seeded):
    with seeded() as s:
        add_block(s, "Madurai", "Wet block", wet=True)
        add_block(s, "Madurai", "Dry block", wet=False)
        mark_block_run(s)
        assert blocks.compute_risk(s, RULES) > 0
        madurai = s.scalar(select(District).where(District.name == "Madurai"))

    today = date.today()
    roll = next(d for d in client.get("/api/outlook").json()["districts"] if d["id"] == madurai.id)
    day = next(x for x in roll["days"] if x["date"] == today.isoformat())
    assert day["level"] == "Low"  # the district HQ is dry...
    assert day["blocks"]["High"] == 1 and day["blocks"]["high_blocks"] == ["Wet block"]  # ...one block is not
    assert roll["action"] == "none"  # and the district rule is unchanged

    detail = client.get(f"/api/districts/{madurai.id}/blocks").json()
    named = [b for b in detail["blocks"] if b["days"]]
    assert [b["name"] for b in named] == ["Wet block", "Dry block"]  # riskiest first
    assert detail["fresh"] is True
    on_map = client.get("/api/blocks").json()["blocks"]
    assert {b["name"]: b["level"] for b in on_map if b["level"]} == {"Wet block": "High", "Dry block": "Low"}
    assert client.get("/api/districts/9999/blocks").status_code == 404


def test_resolve_tolerates_spelling_and_tamil(Session):
    with Session() as s:  # the real seeded block
        b = s.scalar(select(Block).where(Block.name == "Thiruvaiyaru"))
        assert b.name_ta == "திருவையாறு"
        tid = b.district_id
        for name in ("Thiruvaiyaru", "tiruvaiyaru block", "Thiruvayaru", "திருவையாறு"):
            assert blocks.resolve(s, tid, name).id == b.id, name
        assert blocks.resolve(s, tid, "Kumbakonam-xyz") is None
        assert blocks.resolve(s, tid, None) is None


def test_farmer_alerts_follow_their_own_block(seeded):
    with seeded() as s:
        wet = add_block(s, "Madurai", "Wet block", wet=True, name_ta="ஈர ஒன்றியம்")
        add_block(s, "Madurai", "Dry block", wet=False)
        mark_block_run(s)
        blocks.compute_risk(s, RULES)
        madurai = s.scalar(select(District).where(District.name == "Madurai"))
        subs = {}
        for contact, block in (("+91-wet", "Wet block"), ("+91-dry", "Dry block"), ("+91-none", None),
                               ("+91-typo", "Unknown place")):
            sub = Subscriber(contact=contact, channel="outbox", district_id=madurai.id, block=block,
                             variety="CO 51", method="direct_seeded", establish_date=date.today() - timedelta(days=30),
                             language="en", consent_at=datetime.now(), consent_version="test", created_at=datetime.now())
            s.add(sub)
            s.commit()
            subs[contact] = sub.id
        result = farmers.notify_farmers(s, RULES, now=MORNING)
        alerted = {m.subscriber_id: m.body for m in s.scalars(select(FarmerMessage).where(FarmerMessage.kind == "alert"))}
        # Madurai's headquarters is dry, so only the farmer in the wet block is alerted, by block name.
        assert result["alerts"] == 1 and set(alerted) == {subs["+91-wet"]}
        assert alerted[subs["+91-wet"]].startswith("BlastWatch Wet block:")
        assert wet.id in {r.block_id for r in s.scalars(select(BlockRiskDaily).where(BlockRiskDaily.level == "High"))}


def test_stale_block_data_falls_back_to_the_district(seeded):
    with seeded() as s:
        add_block(s, "Thanjavur", "Dry block", wet=False)
        blocks.compute_risk(s, RULES)  # no successful block run recorded: block data is not fresh
        thanjavur = s.scalar(select(District).where(District.name == "Thanjavur"))
        s.add(Subscriber(contact="+91-t", channel="outbox", district_id=thanjavur.id, block="Dry block",
                         variety="CO 51", method="direct_seeded", establish_date=date.today() - timedelta(days=30),
                         language="en", consent_at=datetime.now(), consent_version="test", created_at=datetime.now()))
        s.commit()
        assert not blocks.fresh(s, RULES)
        # Thanjavur's headquarters has wet nights, so the district risk alerts the farmer.
        assert farmers.notify_farmers(s, RULES, now=MORNING)["alerts"] == 1


# ---- District rule A/B, block messages, research endpoints -----------------------------------

def three_blocks(s, district="Madurai", wet=(True, True, False)):
    """Blocks with wet/dry nights in a district whose headquarters is dry (Madurai) or wet (Thanjavur)."""
    made = [add_block(s, district, f"{district} test {i}", w, name_ta=f"சோதனை {i}") for i, w in enumerate(wet)]
    mark_block_run(s)
    blocks.compute_risk(s, RULES)
    return made


def test_rule_b_follows_the_share_of_high_blocks(seeded):
    from blastwatch import alerts

    with seeded() as s:
        three_blocks(s)  # 2 of 3 Madurai blocks wet, headquarters dry
        madurai = s.scalar(select(District).where(District.name == "Madurai"))
        today = date.today()
        rule_a = {**RULES, "alerts": {**RULES["alerts"], "district_rule": "hq"}}
        rule_b = {**RULES, "alerts": {**RULES["alerts"], "district_rule": "blocks_half"}}
        assert alerts.upcoming_high_days(s, madurai.id, rule_a, today) == []
        assert len(alerts.upcoming_high_days(s, madurai.id, rule_b, today)) >= 2
        # Rule B falls back to the headquarters when block data is stale.
        s.query(IngestRun).filter_by(source=blocks.SOURCE).delete()
        s.commit()
        assert alerts.upcoming_high_days(s, madurai.id, rule_b, today) == []


def test_outlook_reports_both_rules(client, seeded):
    with seeded() as s:
        three_blocks(s)
        mid = s.scalar(select(District).where(District.name == "Madurai")).id
    body = client.get("/api/outlook").json()
    assert body["district_rule"] == "hq" and body["blocks_fresh"] is True
    madurai = next(d for d in body["districts"] if d["id"] == mid)
    assert madurai["rule_used"] == "hq" and madurai["action"] == "none"
    assert madurai["rule_actions"] == {"hq": "none", "blocks_half": "alert"}
    assert any(d["level_blocks"] == "High" for d in madurai["days"])
    # A district without scored blocks has no Rule B action.
    erode = next(d for d in body["districts"] if d["name"] == "Erode")
    assert erode["rule_actions"]["blocks_half"] is None


def test_block_table_and_block_message(client, seeded):
    with seeded() as s:
        wet, _, dry = three_blocks(s)
        mid = wet.district_id
        s.add(Subscriber(contact="+91-b", channel="outbox", district_id=mid, block=wet.name, variety="CO 51",
                         method="direct_seeded", establish_date=date.today() - timedelta(days=30), language="ta",
                         consent_at=datetime.now(), consent_version="test", created_at=datetime.now()))
        s.commit()
    table = {b["name"]: b for b in client.get(f"/api/districts/{mid}/blocks").json()["blocks"]}
    w = table[wet.name]
    assert w["max_level"] == "High" and w["alert"] and w["farmers"] == 1 and w["longest_wet_run"] >= 12
    assert table[dry.name]["max_level"] == "Low" and table[dry.name]["farmers"] == 0

    msg = client.get(f"/api/blocks/{wet.id}/message").json()
    assert msg["message"]["ta"].startswith("மதுரை (சோதனை 0 வட்டாரம்):")
    assert msg["message"]["en"].startswith(f"BlastWatch Madurai ({wet.name} block):")
    none = client.get(f"/api/blocks/{dry.id}/message").json()
    assert none["message"] is None and "High days" in none["reason"]
    assert client.get("/api/blocks/999999/message").status_code == 404


def test_compare_rules_and_validation(client, seeded):
    with seeded() as s:
        three_blocks(s)
    cmp = client.get("/api/rules/compare?days=3").json()
    madurai = next(d for d in cmp["districts"] if d["district"] == "Madurai")
    assert madurai["days"] >= 1 and madurai["blocks_high"] >= madurai["hq_high"] == 0

    v = client.get("/api/validation").json()
    assert v["observations"]["present"] == 0 and v["required"]["present"] == 30
    assert v["split"]["holdout_start"] == "2026-10-01" and v["split"]["evaluate_from"] == "2026-10-15"
    assert v["split"]["held_out"] == {"present": 0, "absent": 0, "districts_with_present": 0}
    assert v["audit"]["reports"] == 33 and v["audit"]["by_kind"]["pilot district"] == 1
    assert all(r["report_url"].startswith("https://agritech.tnau.ac.in/") for r in v["audit"]["records"])
