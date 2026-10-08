from datetime import date, datetime, timedelta

import pytest

from blastwatch.risk import (HourObs, leaf_wetness, load_rules, score_series, score_window,
                             susceptibility_for, window_date)

RULES = load_rules()
START = datetime(2025, 11, 9, 12)  # noon; the window ending noon 10 Nov scores as 2025-11-10


def series(hours, temp, rh, precip=0.0, cloud=None, start=START):
    return [HourObs(start + timedelta(hours=i), temp, rh, precip / hours, cloud) for i in range(hours)]


def wet_night(start=START, wet=12):
    """Dry afternoon/evening, then `wet` saturated hours ending at the next noon."""
    dry = series(24 - wet, 31, 60, start=start)
    damp = series(wet, 25, 96, precip=4.0, cloud=85, start=start + timedelta(hours=24 - wet))
    return dry + damp


def test_window_date_runs_noon_to_noon():
    assert window_date(datetime(2025, 11, 9, 12), 12) == date(2025, 11, 10)
    assert window_date(datetime(2025, 11, 10, 11), 12) == date(2025, 11, 10)
    assert window_date(datetime(2025, 11, 10, 12), 12) == date(2025, 11, 11)


def test_long_wet_night_is_high():
    [day] = score_series(wet_night(wet=16), RULES, "default")
    assert day.date == date(2025, 11, 10)
    assert day.longest_wet_run == 16
    assert day.level == "High"


def test_typical_monsoon_night_is_not_high():
    # A 12-hour wet run is the median October-January night, so it must not be High on its own
    # (rules-v2 made it High and was red on 65 % of season days).
    [day] = score_series(wet_night(wet=12), RULES, "default")
    assert day.longest_wet_run == 12 and day.level == "Moderate"


def test_score_points_ramp_from_start_to_full():
    from blastwatch.risk import score_points
    s = RULES["score"]
    assert score_points(s["run_start_hours"], 0, 0, None, RULES)["run"] == 0
    assert score_points(s["full_run_hours"], 0, 0, None, RULES)["run"] == s["run_weight"]
    assert score_points(0, 0, 5.0, 90, RULES) == {"run": 0, "hours": 0, "rain": s["rain_weight"],
                                                  "cloud": s["cloud_weight"]}


def test_dry_day_is_low():
    [day] = score_series(series(24, 33, 55), RULES, "default")
    assert day.wet_hours == 0
    assert day.score == 0
    assert day.level == "Low"


def test_too_hot_or_too_cold_is_not_conducive():
    for temp in (17, 33):
        [day] = score_series(series(24, temp, 98), RULES, "default")
        assert day.wet_hours == 0


def test_overnight_run_is_not_split_at_midnight():
    # Conducive from 20:00 to 08:00 crosses midnight but stays inside one window.
    hours = series(8, 31, 60) + series(12, 24, 95, start=START + timedelta(hours=8)) + \
        series(4, 31, 60, start=START + timedelta(hours=20))
    [day] = score_series(hours, RULES, "default")
    assert day.longest_wet_run == 12


def test_gap_in_data_breaks_the_run():
    hours = wet_night()
    del hours[18]  # missing hour in the middle of the wet spell
    day = score_window(hours, RULES, 1.0, date(2025, 11, 10))
    assert day.longest_wet_run < 12


def test_heavy_rain_gets_no_rain_bonus():
    light = score_window(series(24, 25, 80, precip=5), RULES, 1.0, date(2025, 11, 10))
    heavy = score_window(series(24, 25, 80, precip=60), RULES, 1.0, date(2025, 11, 10))
    assert light.score - heavy.score == RULES["score"]["rain_weight"]


def test_incomplete_window_is_skipped():
    assert score_series(series(10, 25, 96), RULES, "default") == []


def test_susceptibility_scales_score():
    full = score_window(wet_night(), RULES, 1.0, date(2025, 11, 10))
    half = score_window(wet_night(), RULES, 0.5, date(2025, 11, 10))
    assert half.score == pytest.approx(full.score / 2, abs=0.1)


def test_state_month_lookup():
    assert susceptibility_for(RULES, "Tamil Nadu", 10) == 1.0
    assert susceptibility_for(RULES, "Tamil Nadu", 4) == RULES["susceptibility"]["Tamil Nadu"]["default"]
    assert susceptibility_for(RULES, "Unknown State", 4) == 1.0


def hour(**kw):
    return HourObs(START, kw.pop("temp_c", 25.0), kw.pop("rh_pct", None), **kw)


def test_leaf_wetness_prefers_sensor_then_model_then_dew_point_then_rh():
    # A sensor reading wins even when every other signal disagrees.
    assert leaf_wetness(hour(rh_pct=99, leaf_wet_prob=95, leaf_wet_min=0), RULES) == (False, "sensor")
    assert leaf_wetness(hour(rh_pct=60, leaf_wet_prob=70), RULES) == (True, "lwp")
    assert leaf_wetness(hour(rh_pct=99, leaf_wet_prob=10), RULES) == (False, "lwp")
    assert leaf_wetness(hour(rh_pct=80, dew_point_c=23.5), RULES) == (True, "dpd")  # 1.5 C depression
    assert leaf_wetness(hour(rh_pct=80, dew_point_c=20.0), RULES) == (False, "dpd")
    assert leaf_wetness(hour(rh_pct=80, dew_point_c=20.0, precip_mm=1.0), RULES) == (True, "dpd")  # rain
    assert leaf_wetness(hour(rh_pct=95), RULES) == (True, "rh")
    assert leaf_wetness(hour(), RULES) == (False, None)


def test_day_reports_dominant_wetness_basis():
    hours = [HourObs(START + timedelta(hours=i), 25.0, 96.0, leaf_wet_prob=80.0) for i in range(24)]
    [day] = score_series(hours, RULES, "default")
    assert day.wetness_basis == "lwp" and day.longest_wet_run == 24
