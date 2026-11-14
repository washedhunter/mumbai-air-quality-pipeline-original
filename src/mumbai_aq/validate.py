"""Validate a landed day before anything touches the warehouse."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .extract import day_dir, read_manifest
from .parse import PayloadError, parse_archive_csv, parse_locations_json, parse_measurements_json

ALLOWED_STATUS = {"locations": {200}, "measurements": {200}, "archive_day": {200, 403, 404}}
MIN_IN_WINDOW_SHARE = 0.95


class ValidationError(RuntimeError):
    pass


def parse_entry(folder: Path, entry) -> pd.DataFrame | None:
    """Parsed readings for a measurement/archive payload; None for discovery or empty responses."""
    if entry.file is None or entry.endpoint == "locations":
        return None
    body = (folder / entry.file).read_bytes()
    if entry.endpoint == "archive_day":
        return parse_archive_csv(body, entry.location_id)
    return parse_measurements_json(body, entry.location_id, entry.sensor_id)


def validate_day(landing_root: Path, day: date) -> dict:
    folder = day_dir(landing_root, day)
    entries = read_manifest(landing_root, day)
    problems: list[str] = []
    if not entries:
        raise ValidationError(f"{day}: manifest is empty")

    rows = 0
    in_window = 0
    locations_with_data: set[int] = set()
    window = {day - timedelta(days=1), day, day + timedelta(days=1)}
    for e in entries:
        if e.target_date != day.isoformat():
            problems.append(f"{e.file}: target_date {e.target_date} != {day}")
        if e.http_status not in ALLOWED_STATUS.get(e.endpoint, set()):
            problems.append(f"{e.url}: unexpected HTTP {e.http_status} for {e.endpoint}")
        if e.file is None:
            continue
        body = (folder / e.file).read_bytes()
        if hashlib.sha256(body).hexdigest() != e.sha256:
            problems.append(f"{e.file}: sha256 mismatch")
            continue
        try:
            if e.endpoint == "locations":
                if not parse_locations_json(body):
                    problems.append("locations response lists no stations")
                continue
            df = parse_entry(folder, e)
        except PayloadError as exc:
            problems.append(str(exc))
            continue
        assert df is not None
        rows += len(df)
        in_window += int(df["day_ist"].isin(window).sum())
        if len(df):
            locations_with_data.add(int(e.location_id))

    if rows == 0:
        problems.append(f"{day}: no readings in any payload")
    elif in_window / rows < MIN_IN_WINDOW_SHARE:
        problems.append(f"{day}: only {in_window}/{rows} readings fall on or next to the day")
    if problems:
        raise ValidationError("; ".join(problems[:20]))
    return {
        "day": day.isoformat(),
        "payloads": len(entries),
        "readings": rows,
        "locations_with_data": len(locations_with_data),
    }
