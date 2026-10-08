import io
import zipfile
from datetime import datetime

from blastwatch.ingest import faostat, genbank, weather

NOW = datetime(2025, 11, 10, 6)

FAOSTAT_CSV = """Area Code,Area Code (M49),Area,Item Code,Item Code (CPC),Item,Element Code,Element,Unit,Y2023,Y2023F,Y2023N,Y2024,Y2024F,Y2024N
100,'356,India,27,'0113,Rice,5312,Area harvested,ha,48163285.000000,A,,50511930.000000,A,
100,'356,India,27,'0113,Rice,5412,Yield,kg/ha,4313.500000,A,,4313.200000,A,
100,'356,India,27,'0113,Rice,5510,Production,t,207753551.260000,A,,217867864.160000,A,
100,'356,India,15,'0111,Wheat,5510,Production,t,110000000.000000,A,,,,
41,'159,"China, mainland",27,'0113,Rice,5510,Production,t,206000000.000000,A,,,,
"""


def test_faostat_parse_merges_elements_per_year():
    rows = {(r["area"], r["year"]): r for r in faostat.parse_rows(io.StringIO(FAOSTAT_CSV))}
    assert set(rows) == {("India", 2023), ("India", 2024), ("China, mainland", 2023)}
    india = rows[("India", 2024)]
    assert india["area_ha"] == 50511930.0
    assert india["production_t"] == 217867864.16
    assert india["yield_kg_ha"] == 4313.2
    assert rows[("China, mainland", 2023)]["area_ha"] is None  # element missing in source


def test_faostat_read_zip_skips_code_lists(tmp_path):
    path = tmp_path / "bulk.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Production_Crops_Livestock_E_AreaCodes.csv", "junk")
        z.writestr("Production_Crops_Livestock_E_Asia_NOFLAG.csv", "junk")
        z.writestr("Production_Crops_Livestock_E_Asia.csv", FAOSTAT_CSV)
    assert len(faostat.read_zip(path)) == 3


def test_open_meteo_parse_marks_forecast_hours():
    payload = {"hourly": {
        "time": ["2025-11-10T05:00", "2025-11-10T06:00", "2025-11-10T07:00"],
        "temperature_2m": [24.1, 24.0, 25.2],
        "relative_humidity_2m": [96, 97, 90],
        "precipitation": [0.2, 0.0, 0.0],
        "cloud_cover": [100, 90, 80],
    }}
    rows = weather.parse_open_meteo(payload, 7, NOW)
    assert [r["is_forecast"] for r in rows] == [False, False, True]
    assert rows[0]["ts"] == datetime(2025, 11, 10, 5)
    assert rows[0]["district_id"] == 7 and rows[0]["source"] == "open-meteo"


def test_nasa_power_parse_drops_fill_values():
    payload = {"properties": {"parameter": {
        "T2M": {"2023111500": 24.4, "2023111501": -999.0},
        "RH2M": {"2023111500": 96.67, "2023111501": 96.5},
        "PRECTOTCORR": {"2023111500": 0.75, "2023111501": 0.8},
    }}}
    rows = weather.parse_nasa_power(payload, 1, NOW)
    assert rows[0]["ts"] == datetime(2023, 11, 15, 0) and rows[0]["temp_c"] == 24.4
    assert rows[1]["temp_c"] is None and rows[1]["rh_pct"] == 96.5


def test_genbank_summary_parse():
    payload = {"result": {"uids": ["1"], "1": {
        "accessionversion": "KC777366.1", "title": "Oryza sativa Pi54 gene", "slen": 1800,
        "organism": "Oryza sativa", "updatedate": "2014/01/01",
    }}}
    [row] = genbank.parse_summary(payload, gene_id=3)
    assert row == {"gene_id": 3, "accession": "KC777366.1", "title": "Oryza sativa Pi54 gene",
                   "length": 1800, "organism": "Oryza sativa", "update_date": "2014/01/01"}


def test_previous_runs_parse_uses_lead_suffix_and_drops_uncovered_hours():
    payload = {"hourly": {
        "time": ["2024-01-21T23:00", "2024-01-22T00:00"],
        "temperature_2m_previous_day3": [24.0, 23.5],
        "relative_humidity_2m_previous_day3": [None, 96.0],
        "dew_point_2m_previous_day3": [None, 22.8],
        "precipitation_previous_day3": [0.0, 0.3],
        "cloud_cover_previous_day3": [80.0, 95.0],
        "leaf_wetness_probability_previous_day3": [None, 70.0],
    }}
    [row] = weather.parse_previous_runs(payload, 4, NOW, lead_days=3)
    assert row["ts"] == datetime(2024, 1, 22, 0) and row["source"] == "archive-forecast:d3"
    assert row["rh_pct"] == 96.0 and row["leaf_wet_prob"] == 70.0 and row["is_forecast"] is True
