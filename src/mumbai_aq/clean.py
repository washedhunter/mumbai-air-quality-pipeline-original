"""Clean and standardise raw readings into hourly station concentrations.

Input: staging rows (one per reading). Output: hourly means per station and pollutant in standard
units (µg/m³; CO in mg/m³), with IST hour starts, plus a count of rows dropped per reason.

Everything here works on one IST day at a time, so the result for a day depends only on that
day's readings (re-running a day, or loading days in a different order, gives the same rows).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import IST

POLLUTANTS = ("pm25", "pm10", "no2", "so2", "o3", "co", "nh3")

# ppb/ppm -> µg/m³ (CO -> mg/m³) at 25 °C and 1 atm: factor = molar mass / 24.45.
TRUE_PPB_FACTOR = {"no2": 1.88, "so2": 2.62, "o3": 1.96, "nh3": 0.70, "co": 0.001145}

SENTINELS = frozenset({-9999.0, -999.0, 999.0, 9999.0, 99999.0})
# Upper plausibility limits in standard units (anything above is an instrument/transfer error).
# PM >= 999 is excluded by CPCB itself.
MAX_PLAUSIBLE = {
    "pm25": 999.0,
    "pm10": 999.0,
    "no2": 1000.0,
    "so2": 2000.0,
    "o3": 1000.0,
    "co": 50.0,
    "nh3": 2000.0,
}
# Isolated spikes: value > 3x the rolling 2-hour median AND above it by at least this much.
SPIKE_MIN_JUMP = {
    "pm25": 150.0,
    "pm10": 250.0,
    "no2": 150.0,
    "so2": 150.0,
    "o3": 150.0,
    "co": 5.0,
    "nh3": 200.0,
}
FLATLINE_MIN_SPAN = pd.Timedelta(hours=5)  # first..last reading of an identical run (≈ 6 h of data)
FLATLINE_MIN_READINGS = 4
HOURLY_CADENCE = pd.Timedelta(minutes=50)  # sensors reporting this rarely need 1 reading per hour


@dataclass
class CleanResult:
    hourly: pd.DataFrame
    dropped: Counter = field(default_factory=Counter)  # (parameter, reason) -> rows


def unit_factor(parameter: str, units: str, ppb_label_means: str = "cpcb_native") -> float | None:
    """Multiplier from the reported unit to the standard unit, or None if unknown."""
    u = units.strip().lower().replace("μ", "µ").replace("ug/m3", "µg/m³").replace("mg/m3", "mg/m³")
    if parameter == "co":
        if u == "mg/m³":
            return 1.0
        if u == "µg/m³":
            return 0.001
        if u in {"ppb", "ppm"}:
            if ppb_label_means == "cpcb_native":
                return 1.0  # CPCB publishes CO in mg/m³; OpenAQ mislabels it (see DECISIONS.md)
            return TRUE_PPB_FACTOR["co"] * (1000.0 if u == "ppm" else 1.0)
        return None
    if u == "µg/m³":
        return 1.0
    if u == "mg/m³":
        return 1000.0
    if u in {"ppb", "ppm"} and parameter in TRUE_PPB_FACTOR:
        if ppb_label_means == "cpcb_native":
            return 1.0
        return TRUE_PPB_FACTOR[parameter] * (1000.0 if u == "ppm" else 1.0)
    return None


def hour_start_ist(ts_utc: pd.Series) -> pd.Series:
    """Readings are stamped at the end of their averaging period: 10:15 covers 10:00-10:15,
    11:00 covers 10:45-11:00, so both belong to the 10:00 hour. Returns naive IST hour starts."""
    local = pd.to_datetime(ts_utc, utc=True).dt.tz_convert(IST).dt.tz_localize(None)
    return (local - pd.Timedelta(minutes=1)).dt.floor("h")


def _flatline_mask(group: pd.DataFrame) -> pd.Series:
    """True for readings inside a run of identical values lasting >= ~6 hours."""
    values = group["value"].to_numpy()
    run_id = np.concatenate([[0], np.cumsum(values[1:] != values[:-1])])
    ts = group["ts"]
    runs = pd.DataFrame({"run": run_id, "ts": ts.to_numpy()})
    agg = runs.groupby("run")["ts"].agg(["min", "max", "count"])
    bad_runs = agg[
        ((agg["max"] - agg["min"]) >= FLATLINE_MIN_SPAN) & (agg["count"] >= FLATLINE_MIN_READINGS)
    ].index
    return pd.Series(np.isin(run_id, bad_runs), index=group.index)


def _spike_mask(group: pd.DataFrame, parameter: str) -> pd.Series:
    median = group["value"].rolling(9, center=True, min_periods=3).median()
    jump = group["value"] - median
    return (group["value"] > 3 * median) & (jump > SPIKE_MIN_JUMP[parameter])


def clean_readings(
    readings: pd.DataFrame,
    stations: pd.DataFrame,
    ppb_label_means: str = "cpcb_native",
) -> CleanResult:
    """readings: location_id, sensor_id, parameter, units, value, ts_utc.
    stations: location_id -> station_key mapping (unmapped locations become 'loc_<id>')."""
    dropped: Counter = Counter()
    empty = pd.DataFrame(
        columns=[
            "station_key",
            "parameter",
            "hour_ist",
            "day_ist",
            "value",
            "n_readings",
            "n_sensors",
        ]
    )
    df = readings.copy()
    df = df[df["parameter"].isin(POLLUTANTS)]
    if df.empty:
        return CleanResult(empty, dropped)

    def drop(mask: pd.Series, reason: str) -> None:
        nonlocal df
        mask = mask.reindex(df.index, fill_value=False).astype(bool)
        if mask.any():
            for param, n in df.loc[mask, "parameter"].value_counts().items():
                dropped[(param, reason)] += int(n)
            df = df[~mask]

    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    drop(df["value"].isna(), "missing_value")

    factors = [
        unit_factor(p, u, ppb_label_means)
        for p, u in zip(df["parameter"], df["units"], strict=True)
    ]
    df["factor"] = pd.array(factors, dtype="Float64")
    drop(df["factor"].isna(), "unknown_unit")
    drop(df["value"].isin(SENTINELS), "sentinel")
    drop(df["value"] < 0, "negative")
    drop(df["parameter"].isin(["pm25", "pm10"]) & (df["value"] == 0), "zero_pm")
    df["value"] = (df["value"] * df["factor"]).astype(float)
    limit = df["parameter"].map(MAX_PLAUSIBLE)
    drop(df["value"] >= limit, "implausible")

    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.sort_values(["sensor_id", "location_id", "parameter", "ts"])
    dup = df.duplicated(subset=["location_id", "sensor_id", "parameter", "ts"], keep="last")
    drop(dup, "duplicate_timestamp")

    keys = ["location_id", "sensor_id", "parameter"]
    flat = pd.Series(False, index=df.index)
    spike = pd.Series(False, index=df.index)
    for (_, _, param), group in df.groupby(keys, sort=False, dropna=False):
        flat.loc[group.index] = _flatline_mask(group)
        spike.loc[group.index] = _spike_mask(group, param) & ~flat.loc[group.index]
    drop(flat, "flatline")
    drop(spike, "spike")
    if df.empty:
        return CleanResult(empty, dropped)

    df["hour_ist"] = hour_start_ist(df["ts"])
    # Sub-hourly sensors need >= 2 readings in an hour; hourly sensors need 1.
    gap_s = df.groupby(keys, dropna=False)["ts"].diff().dt.total_seconds()
    cadence_s = gap_s.groupby([df[k] for k in keys], dropna=False).transform("median")
    # A sensor with a single reading has no known cadence: treat it as sub-hourly (strict).
    hourly_sensor = cadence_s.notna() & (cadence_s >= HOURLY_CADENCE.total_seconds())
    df["min_n"] = np.where(hourly_sensor, 1, 2)
    per_sensor = (
        df.groupby([*keys, "hour_ist"], dropna=False)
        .agg(value=("value", "mean"), n_readings=("value", "size"), min_n=("min_n", "max"))
        .reset_index()
    )
    thin = per_sensor["n_readings"] < per_sensor["min_n"]
    for param, n in per_sensor.loc[thin].groupby("parameter")["n_readings"].sum().items():
        dropped[(param, "too_few_readings_in_hour")] += int(n)
    per_sensor = per_sensor[~thin]

    mapping = stations.set_index("location_id")["station_key"]
    per_sensor["station_key"] = [
        mapping.get(loc, f"loc_{loc}") for loc in per_sensor["location_id"]
    ]
    hourly = (
        per_sensor.groupby(["station_key", "parameter", "hour_ist"])
        .agg(
            value=("value", "mean"),
            n_readings=("n_readings", "sum"),
            n_sensors=("value", "size"),
        )
        .reset_index()
    )
    hourly["day_ist"] = hourly["hour_ist"].dt.date
    hourly["value"] = hourly["value"].round(4)
    return CleanResult(
        hourly[
            ["station_key", "parameter", "hour_ist", "day_ist", "value", "n_readings", "n_sensors"]
        ],
        dropped,
    )
