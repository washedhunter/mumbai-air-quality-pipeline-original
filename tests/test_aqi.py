"""CPCB NAQI: breakpoint values, interpolation, averaging windows and the minimum-pollutant rule."""

import math

import pandas as pd
import pytest

from mumbai_aq import aqi


@pytest.mark.parametrize(
    ("pollutant", "conc", "expected"),
    [
        # Band edges from the CPCB table land exactly on the AQI band edges.
        ("pm25", 0, 0),
        ("pm25", 30, 50),
        ("pm25", 31, 51),
        ("pm25", 60, 100),
        ("pm25", 61, 101),
        ("pm25", 90, 200),
        ("pm25", 120, 300),
        ("pm25", 250, 400),
        ("pm10", 50, 50),
        ("pm10", 100, 100),
        ("pm10", 250, 200),
        ("pm10", 350, 300),
        ("pm10", 430, 400),
        ("no2", 40, 50),
        ("no2", 80, 100),
        ("no2", 180, 200),
        ("no2", 280, 300),
        ("no2", 400, 400),
        ("so2", 40, 50),
        ("so2", 80, 100),
        ("so2", 380, 200),
        ("so2", 800, 300),
        ("so2", 1600, 400),
        ("o3", 50, 50),
        ("o3", 100, 100),
        ("o3", 168, 200),
        ("o3", 208, 300),
        ("o3", 748, 400),
        ("co", 1.0, 50),
        ("co", 2.0, 100),
        ("co", 10, 200),
        ("co", 17, 300),
        ("co", 34, 400),
        ("nh3", 200, 50),
        ("nh3", 400, 100),
        ("nh3", 800, 200),
        ("nh3", 1200, 300),
        ("nh3", 1800, 400),
    ],
)
def test_band_edges(pollutant, conc, expected):
    assert aqi.round_aqi(aqi.sub_index(pollutant, conc)) == expected


def test_worked_example_pm25_45_is_75():
    # (100 - 51) / (60 - 31) * (45 - 31) + 51 = 74.66 -> 75 (CPCB / IES worked example)
    value = aqi.sub_index("pm25", 45)
    assert value == pytest.approx(74.655, abs=1e-3)
    assert aqi.round_aqi(value) == 75


def test_interpolation_inside_band():
    # PM10 175 in the 101-250 band (AQI 101-200): (200-101)/(250-101)*(175-101)+101
    assert aqi.sub_index("pm10", 175) == pytest.approx(99 / 149 * 74 + 101)


def test_gap_between_bands_stays_monotonic():
    a, b, c = (aqi.sub_index("pm25", x) for x in (30, 30.5, 31))
    assert a <= b <= c
    assert aqi.round_aqi(b) == 50


def test_severe_band_and_cap():
    assert aqi.round_aqi(aqi.sub_index("pm25", 251)) == 401
    assert aqi.round_aqi(aqi.sub_index("pm25", 380)) == 500
    assert aqi.sub_index("pm25", 900) == 500.0
    assert aqi.sub_index("co", 80) == 500.0


def test_missing_and_negative():
    assert aqi.sub_index("pm25", None) is None
    assert aqi.sub_index("pm25", math.nan) is None
    assert aqi.sub_index("pm25", -1) is None


@pytest.mark.parametrize(
    ("value", "name"),
    [
        (0, "Good"),
        (50, "Good"),
        (51, "Satisfactory"),
        (100, "Satisfactory"),
        (101, "Moderate"),
        (200, "Moderate"),
        (201, "Poor"),
        (301, "Very Poor"),
        (401, "Severe"),
        (500, "Severe"),
    ],
)
def test_category(value, name):
    assert aqi.category(value) == name


def test_round_half_up():
    assert aqi.round_aqi(100.5) == 101
    assert aqi.round_aqi(100.49) == 100


def test_overall_needs_three_pollutants_including_pm():
    ok = aqi.overall_aqi({"pm25": 120.0, "no2": 40.0, "so2": 10.0})
    assert ok.aqi == 120 and ok.dominant == "pm25" and ok.category == "Moderate"

    two = aqi.overall_aqi({"pm25": 120.0, "no2": 40.0})
    assert two.aqi is None and "need 3" in two.reason

    no_pm = aqi.overall_aqi({"no2": 120.0, "so2": 40.0, "o3": 30.0})
    assert no_pm.aqi is None and "PM" in no_pm.reason

    missing_ignored = aqi.overall_aqi({"pm10": 80.0, "no2": 90.0, "co": 50.0, "o3": None})
    assert missing_ignored.aqi == 90 and missing_ignored.dominant == "no2"


def test_overall_tie_breaks_alphabetically():
    assert aqi.overall_aqi({"pm25": 100.0, "pm10": 100.0, "no2": 20.0}).dominant == "pm10"


def _hours(day: str, values, start_hour=0):
    idx = pd.date_range(
        pd.Timestamp(day) + pd.Timedelta(hours=start_hour), periods=len(values), freq="h"
    )
    return pd.Series(values, index=idx, dtype=float)


def test_24h_mean_needs_16_hours():
    assert aqi.daily_concentration("pm25", _hours("2026-01-10", [50.0] * 16)) == 50.0
    assert aqi.daily_concentration("pm25", _hours("2026-01-10", [50.0] * 15)) is None


def test_8h_max_rolling_mean():
    values = [10.0] * 24
    values[12:20] = [100.0] * 8  # an 8-hour block of 100
    assert aqi.daily_concentration("o3", _hours("2026-01-10", values)) == pytest.approx(100.0)


def test_8h_window_uses_previous_evening_and_needs_6_hours():
    prev = _hours("2026-01-09", [200.0] * 7, start_hour=17)  # 17:00-23:00 the day before
    day = _hours("2026-01-10", [10.0] * 20)  # 00:00-19:00
    s = pd.concat([prev, day])
    # Window ending 00:00 covers 17:00-00:00: (7*200 + 10) / 8
    assert aqi.daily_concentration("co", s) == pytest.approx((7 * 200 + 10) / 8)


def test_8h_needs_16_hours_in_day():
    assert aqi.daily_concentration("co", _hours("2026-01-10", [1.0] * 15)) is None


def test_day_aqi_end_to_end():
    day = "2026-01-10"
    res = aqi.day_aqi(
        {
            "pm25": _hours(day, [45.0] * 24),
            "pm10": _hours(day, [100.0] * 24),
            "no2": _hours(day, [20.0] * 24),
            "temperature": _hours(day, [25.0] * 24),  # ignored: not an AQI pollutant
        }
    )
    assert res.aqi == 100 and res.dominant == "pm10"
    assert res.sub_indices == {"pm25": 75, "pm10": 100, "no2": 25}


def test_breakpoint_rows_cover_every_band():
    rows = list(aqi.breakpoint_rows())
    assert len(rows) == 6 * len(aqi.BREAKPOINTS)
    assert {r[0] for r in rows} == set(aqi.BREAKPOINTS)
