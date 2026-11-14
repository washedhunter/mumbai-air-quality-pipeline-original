"""Turn raw payload bytes into staging rows. No cleaning here, only decoding."""

from __future__ import annotations

import csv
import gzip
import io
import json
from datetime import UTC

import pandas as pd

from .clean import hour_start_ist

STAGING_COLUMNS = ["location_id", "sensor_id", "parameter", "units", "value", "ts_utc", "day_ist"]
ARCHIVE_COLUMNS = {"location_id", "sensors_id", "datetime", "parameter", "units", "value"}


class PayloadError(ValueError):
    pass


def fix_units(units: str) -> str:
    """The API double-encodes UTF-8 ("Âµg/m³" for "µg/m³"); undo that when it happens."""
    if "Â" in units or "Ã" in units:
        try:
            return units.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return units
    return units


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=STAGING_COLUMNS[:-1])
    if df.empty:
        return pd.DataFrame(columns=STAGING_COLUMNS)
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    df["day_ist"] = hour_start_ist(df["ts_utc"]).dt.date
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["units"] = df["units"].map(fix_units)
    return df[STAGING_COLUMNS]


def parse_archive_csv(body: bytes, location_id: int) -> pd.DataFrame:
    try:
        text = gzip.decompress(body).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PayloadError(
            f"archive file for location {location_id} is not valid gzip/UTF-8"
        ) from exc
    reader = csv.DictReader(io.StringIO(text))
    missing = ARCHIVE_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise PayloadError(
            f"archive file for location {location_id} lacks columns {sorted(missing)}"
        )
    rows = []
    for rec in reader:
        if int(rec["location_id"]) != location_id:
            raise PayloadError(
                f"archive file for {location_id} contains location {rec['location_id']}"
            )
        rows.append(
            {
                "location_id": location_id,
                "sensor_id": int(rec["sensors_id"]) if rec["sensors_id"] else None,
                "parameter": rec["parameter"],
                "units": rec["units"],
                "value": rec["value"],
                "ts_utc": rec["datetime"],
            }
        )
    return _frame(rows)


def parse_measurements_json(body: bytes, location_id: int, sensor_id: int) -> pd.DataFrame:
    try:
        doc = json.loads(body)
        results = doc["results"]
    except (ValueError, KeyError, TypeError) as exc:
        raise PayloadError(
            f"measurements for sensor {sensor_id} are not valid OpenAQ JSON"
        ) from exc
    rows = []
    for rec in results:
        try:
            rows.append(
                {
                    "location_id": location_id,
                    "sensor_id": sensor_id,
                    "parameter": rec["parameter"]["name"],
                    "units": rec["parameter"]["units"],
                    "value": rec["value"],
                    "ts_utc": rec["period"]["datetimeTo"]["utc"],
                }
            )
        except (KeyError, TypeError) as exc:
            raise PayloadError(f"unexpected measurement shape for sensor {sensor_id}") from exc
    return _frame(rows)


def parse_locations_json(body: bytes) -> list[dict]:
    try:
        results = json.loads(body)["results"]
    except (ValueError, KeyError, TypeError) as exc:
        raise PayloadError("locations response is not valid OpenAQ JSON") from exc
    out = []
    for loc in results:
        coords = loc.get("coordinates") or {}
        first = (loc.get("datetimeFirst") or {}).get("utc")
        last = (loc.get("datetimeLast") or {}).get("utc")
        out.append(
            {
                "location_id": int(loc["id"]),
                "name": loc.get("name"),
                "provider": (loc.get("provider") or {}).get("name"),
                "lat": coords.get("latitude"),
                "lon": coords.get("longitude"),
                "datetime_first": pd.Timestamp(first).tz_convert(UTC) if first else None,
                "datetime_last": pd.Timestamp(last).tz_convert(UTC) if last else None,
                "sensors": [
                    {
                        "id": int(s["id"]),
                        "parameter": s["parameter"]["name"],
                        "units": fix_units(s["parameter"].get("units") or ""),
                    }
                    for s in loc.get("sensors", [])
                ],
            }
        )
    return out
