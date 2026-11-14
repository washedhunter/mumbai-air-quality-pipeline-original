"""CPCB National Air Quality Index (NAQI, 2014 method). Reference implementation.

The SQL models compute the same thing from a breakpoint table generated from BREAKPOINTS below,
and a test checks that both agree.

Rules implemented (CPCB, "National Air Quality Index", 2014):
- Sub-index by linear interpolation inside the pollutant's breakpoint band:
  I = (I_hi - I_lo) / (B_hi - B_lo) * (C - B_lo) + I_lo
- Averaging: 24-hour mean for PM10, PM2.5, NO2, SO2, NH3; maximum 8-hour rolling mean for CO, O3.
- A sub-index needs at least 16 hours of data in the day; an 8-hour mean needs >= 6 of its 8 hours.
- Overall AQI only if >= 3 pollutants have a sub-index and one of them is PM2.5 or PM10;
  AQI = the highest sub-index, and that pollutant is the dominant (prominent) pollutant.

Units: µg/m³ for everything except CO (mg/m³).

CPCB's table leaves the Severe band (401-500) open-ended ("430+"). For a finite upper breakpoint
we use the convention common in Indian AQI implementations (e.g. PM2.5 500 at 380 µg/m³);
anything above it is capped at 500. Lead (Pb) is omitted: no Mumbai station reports it on OpenAQ.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pandas as pd

AQI_BANDS: list[tuple[int, int]] = [
    (0, 50),
    (51, 100),
    (101, 200),
    (201, 300),
    (301, 400),
    (401, 500),
]
CATEGORIES = ["Good", "Satisfactory", "Moderate", "Poor", "Very Poor", "Severe"]

# (B_lo, B_hi) per AQI band, in the order of AQI_BANDS.
BREAKPOINTS: dict[str, list[tuple[float, float]]] = {
    "pm10": [(0, 50), (51, 100), (101, 250), (251, 350), (351, 430), (431, 510)],
    "pm25": [(0, 30), (31, 60), (61, 90), (91, 120), (121, 250), (251, 380)],
    "no2": [(0, 40), (41, 80), (81, 180), (181, 280), (281, 400), (401, 520)],
    "o3": [(0, 50), (51, 100), (101, 168), (169, 208), (209, 748), (749, 1028)],
    "co": [(0, 1.0), (1.1, 2.0), (2.1, 10), (10.1, 17), (17.1, 34), (34.1, 51)],
    "so2": [(0, 40), (41, 80), (81, 380), (381, 800), (801, 1600), (1601, 2400)],
    "nh3": [(0, 200), (201, 400), (401, 800), (801, 1200), (1201, 1800), (1801, 2400)],
}

UNITS = {p: ("mg/m³" if p == "co" else "µg/m³") for p in BREAKPOINTS}
EIGHT_HOUR = frozenset({"co", "o3"})
PM = frozenset({"pm25", "pm10"})
MIN_HOURS_PER_DAY = 16
MIN_HOURS_PER_8H = 6
MIN_POLLUTANTS = 3


def sub_index(pollutant: str, concentration: float | None) -> float | None:
    """Sub-index (unrounded) for one pollutant concentration; None if missing or negative."""
    if concentration is None or (isinstance(concentration, float) and math.isnan(concentration)):
        return None
    if concentration < 0:
        return None
    bands = BREAKPOINTS[pollutant]
    for (b_lo, b_hi), (i_lo, i_hi) in zip(bands, AQI_BANDS, strict=True):
        if concentration <= b_hi:
            value = (i_hi - i_lo) / (b_hi - b_lo) * (concentration - b_lo) + i_lo
            # Concentrations in the small gap between bands (e.g. PM2.5 30.4) land just below I_lo.
            return max(value, float(i_lo) - 1 if i_lo > 0 else 0.0)
    return 500.0


def round_aqi(value: float) -> int:
    """Round half up (CPCB reports whole numbers)."""
    return math.floor(value + 0.5)


def category(aqi: float | None) -> str | None:
    if aqi is None:
        return None
    rounded = round_aqi(aqi)
    for (_, hi), name in zip(AQI_BANDS, CATEGORIES, strict=True):
        if rounded <= hi:
            return name
    return CATEGORIES[-1]


@dataclass(frozen=True)
class AqiResult:
    aqi: int | None
    category: str | None
    dominant: str | None
    sub_indices: dict[str, int]
    reason: str | None = None  # why the AQI is missing, if it is


def overall_aqi(sub_indices: Mapping[str, float | None]) -> AqiResult:
    """Combine sub-indices with CPCB's minimum-pollutant rule."""
    valid = {p: v for p, v in sub_indices.items() if v is not None}
    rounded = {p: round_aqi(v) for p, v in valid.items()}
    if len(valid) < MIN_POLLUTANTS:
        return AqiResult(None, None, None, rounded, f"only {len(valid)} pollutants (need 3)")
    if not PM & valid.keys():
        return AqiResult(None, None, None, rounded, "no PM2.5 or PM10")
    # Ties go to the alphabetically first pollutant so the result is deterministic.
    dominant = sorted(valid, key=lambda p: (-valid[p], p))[0]
    value = round_aqi(valid[dominant])
    return AqiResult(value, category(value), dominant, rounded)


def daily_concentration(pollutant: str, hourly: pd.Series) -> float | None:
    """Daily concentration from one day's hourly means (index: hour start, IST).

    24-hour pollutants: mean of the day's hourly values (needs >= 16 hours).
    CO / O3: maximum of 8-hour rolling means ending at each hour of the day (each window needs
    >= 6 hours); `hourly` may include up to 7 hours before the day so early windows are complete.
    The day is taken as the date of the last timestamp in the series. Rounded to 4 decimals
    (the SQL model does the same, so results don't depend on summation order).
    """
    hourly = hourly.dropna().sort_index()
    if hourly.empty:
        return None
    day = hourly.index.max().normalize()
    in_day = hourly[hourly.index >= day]
    if len(in_day) < MIN_HOURS_PER_DAY:
        return None
    if pollutant not in EIGHT_HOUR:
        return round(float(in_day.mean()), 4)
    full_index = pd.date_range(day - pd.Timedelta(hours=7), day + pd.Timedelta(hours=23), freq="h")
    regular = hourly.reindex(full_index)
    rolling = regular.rolling(8, min_periods=MIN_HOURS_PER_8H).mean()
    # Windows end at hours of the day that have data (matches the SQL window function).
    windows = rolling[(rolling.index >= day) & regular.notna()].dropna()
    return round(float(windows.max()), 4) if not windows.empty else None


def day_aqi(hourly_by_pollutant: Mapping[str, pd.Series]) -> AqiResult:
    """Daily AQI for one station from hourly series per pollutant."""
    subs = {
        p: sub_index(p, daily_concentration(p, s))
        for p, s in hourly_by_pollutant.items()
        if p in BREAKPOINTS
    }
    return overall_aqi(subs)


def breakpoint_rows() -> Iterable[tuple[str, int, float, float, int, int, str]]:
    """Rows for the SQL reference table core.aqi_breakpoints."""
    for pollutant, bands in BREAKPOINTS.items():
        for band, ((b_lo, b_hi), (i_lo, i_hi), name) in enumerate(
            zip(bands, AQI_BANDS, CATEGORIES, strict=True), start=1
        ):
            yield pollutant, band, float(b_lo), float(b_hi), i_lo, i_hi, name
