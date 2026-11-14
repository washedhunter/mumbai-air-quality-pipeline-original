import pandas as pd
import pytest

from mumbai_aq.clean import clean_readings, hour_start_ist, unit_factor

STATIONS = pd.DataFrame({"location_id": [1, 2, 3], "station_key": ["a", "a", "b"]})


def readings(rows):
    """rows: (location_id, sensor_id, parameter, units, value, IST timestamp string)."""
    df = pd.DataFrame(
        rows, columns=["location_id", "sensor_id", "parameter", "units", "value", "ts"]
    )
    df["ts_utc"] = pd.to_datetime(df.pop("ts")).dt.tz_localize("Asia/Kolkata").dt.tz_convert("UTC")
    return df


def quarter_hours(loc, sensor, param, units, values, start="2026-01-10 00:15"):
    ts = pd.date_range(start, periods=len(values), freq="15min")
    return [(loc, sensor, param, units, v, str(t)) for v, t in zip(values, ts, strict=True)]


@pytest.mark.parametrize(
    ("param", "units", "mode", "factor"),
    [
        ("pm25", "µg/m³", "cpcb_native", 1.0),
        ("pm25", "Âµg/m³", "cpcb_native", None),  # mojibake is fixed in parse.py, not here
        ("co", "µg/m³", "cpcb_native", 0.001),
        ("co", "mg/m³", "cpcb_native", 1.0),
        ("co", "ppb", "cpcb_native", 1.0),
        ("co", "ppb", "true_ppb", 0.001145),
        ("no2", "ppb", "cpcb_native", 1.0),
        ("no2", "ppb", "true_ppb", 1.88),
        ("so2", "ppm", "true_ppb", 2620.0),
        ("o3", "ug/m3", "cpcb_native", 1.0),
        ("pm10", "furlongs", "cpcb_native", None),
    ],
)
def test_unit_factor(param, units, mode, factor):
    got = unit_factor(param, units, mode)
    assert got == (pytest.approx(factor) if factor is not None else None)


def test_hour_start_uses_period_end():
    ts = pd.Series(pd.to_datetime(["2026-01-10 10:15", "2026-01-10 11:00", "2026-01-11 00:00"]))
    ts = ts.dt.tz_localize("Asia/Kolkata").dt.tz_convert("UTC")
    hours = hour_start_ist(ts)
    assert list(hours.astype(str)) == [
        "2026-01-10 10:00:00",
        "2026-01-10 10:00:00",
        "2026-01-10 23:00:00",
    ]


def test_hourly_mean_and_station_merge():
    rows = quarter_hours(1, 11, "pm25", "µg/m³", [40, 50, 60, 70])  # 00:15..01:00 -> hour 00
    rows += quarter_hours(2, 21, "pm25", "µg/m³", [100, 100, 100, 100])  # same station "a"
    res = clean_readings(readings(rows), STATIONS)
    h = res.hourly
    assert len(h) == 1
    row = h.iloc[0]
    assert row.station_key == "a" and str(row.hour_ist) == "2026-01-10 00:00:00"
    assert row.value == pytest.approx((55 + 100) / 2)  # mean of the two sensors' hourly means
    assert row.n_readings == 8 and row.n_sensors == 2


def test_drops_sentinels_negatives_zero_pm_and_implausible():
    rows = quarter_hours(3, 31, "pm10", "µg/m³", [999, -5, 0, 80, 85, 90, 1500, 95])
    res = clean_readings(readings(rows), STATIONS)
    reasons = {r: n for (_, r), n in res.dropped.items()}
    # 80 (01:00) is alone in the 00:00 hour after the drops, so that hour is incomplete.
    assert reasons == {
        "sentinel": 1,
        "negative": 1,
        "zero_pm": 1,
        "implausible": 1,
        "too_few_readings_in_hour": 1,
    }
    assert res.hourly["value"].tolist() == [pytest.approx(90.0)]  # 85, 90, 95 in the 01:00 hour


def test_converts_units_to_standard():
    rows = quarter_hours(3, 32, "co", "µg/m³", [1200, 1000, 800, 1000])
    res = clean_readings(readings(rows), STATIONS)
    assert res.hourly["value"].iloc[0] == pytest.approx(1.0)  # mg/m³


def test_flatline_of_six_hours_is_removed():
    stuck = [29.26] * 24  # 6 hours of an identical value at 15-minute cadence
    varied = [10.0, 11.0, 12.0, 13.0]
    rows = quarter_hours(3, 33, "so2", "ppb", varied + stuck + varied)
    res = clean_readings(readings(rows), STATIONS)
    assert res.dropped[("so2", "flatline")] == 24
    assert len(res.hourly) == 2


def test_short_repeats_are_kept():
    rows = quarter_hours(3, 34, "pm25", "µg/m³", [50, 50, 50, 50, 51, 51, 51, 51])
    res = clean_readings(readings(rows), STATIONS)
    assert sum(res.dropped.values()) == 0 and len(res.hourly) == 2


def test_isolated_spike_is_removed_but_sustained_rise_is_kept():
    base = [40.0] * 12
    spiky = [*base[:6], 600.0, *base[6:]]
    res = clean_readings(readings(quarter_hours(3, 35, "pm25", "µg/m³", spiky)), STATIONS)
    assert res.dropped[("pm25", "spike")] == 1

    rise = [40.0] * 6 + [300.0] * 8
    res = clean_readings(readings(quarter_hours(3, 36, "pm25", "µg/m³", rise)), STATIONS)
    assert res.dropped[("pm25", "spike")] == 0


def test_hour_needs_two_quarter_hours_but_hourly_sensors_need_one():
    rows = quarter_hours(3, 37, "no2", "µg/m³", [20.0])  # one reading in the hour
    res = clean_readings(readings(rows), STATIONS)
    assert res.hourly.empty and res.dropped[("no2", "too_few_readings_in_hour")] == 1

    ts = pd.date_range("2026-01-10 01:00", periods=3, freq="h")
    hourly_rows = [(3, 38, "pm25", "µg/m³", 30.0 + i, str(t)) for i, t in enumerate(ts)]
    res = clean_readings(readings(hourly_rows), STATIONS)
    assert len(res.hourly) == 3


def test_duplicate_timestamps_and_unmapped_locations():
    rows = quarter_hours(9, 91, "pm25", "µg/m³", [10, 20]) * 2
    res = clean_readings(readings(rows), STATIONS)
    assert res.dropped[("pm25", "duplicate_timestamp")] == 2
    assert res.hourly["station_key"].tolist() == ["loc_9"]


def test_ignores_non_aqi_parameters():
    rows = quarter_hours(3, 39, "temperature", "c", [25, 26])
    res = clean_readings(readings(rows), STATIONS)
    assert res.hourly.empty and not res.dropped
