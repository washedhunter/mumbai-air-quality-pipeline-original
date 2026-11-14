"""Load a validated landing folder into raw + staging, and clean staging into core.hourly.

Every write is scoped to one day and done as delete-then-insert (or upsert) inside a single
transaction, so re-running a day never duplicates rows.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from .clean import POLLUTANTS, clean_readings
from .config import Settings
from .extract import day_dir, read_manifest
from .parse import parse_locations_json
from .validate import parse_entry


def load_raw(conn: psycopg.Connection, landing_root: Path, day: date) -> dict:
    folder = day_dir(landing_root, day)
    entries = read_manifest(landing_root, day)
    staged = 0
    with conn.transaction(), conn.cursor() as cur:
        # Readings for this day get replaced wholesale (also if the source changed api -> archive).
        cur.execute("DELETE FROM staging.measurements WHERE target_date = %s", (day,))
        for e in entries:
            body = (folder / e.file).read_bytes() if e.file else None
            cur.execute(
                """INSERT INTO raw.payloads (source, endpoint, location_id, sensor_id, target_date,
                       url, http_status, content_type, body, sha256, fetched_at, loaded_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT ON CONSTRAINT payloads_natural_key DO UPDATE SET
                       url = EXCLUDED.url, http_status = EXCLUDED.http_status,
                       content_type = EXCLUDED.content_type, body = EXCLUDED.body,
                       sha256 = EXCLUDED.sha256, fetched_at = EXCLUDED.fetched_at,
                       loaded_at = now()
                   RETURNING payload_id""",
                (
                    e.source,
                    e.endpoint,
                    e.location_id,
                    e.sensor_id,
                    day,
                    e.url,
                    e.http_status,
                    e.content_type,
                    body,
                    e.sha256,
                    datetime.fromisoformat(e.fetched_at),
                ),
            )
            payload_id = cur.fetchone()[0]
            if e.endpoint == "locations" and body is not None:
                _upsert_locations(cur, parse_locations_json(body), day)
                continue
            df = parse_entry(folder, e)
            if df is None or df.empty:
                continue
            # raw keeps every parameter; staging only the AQI pollutants (halves its size).
            df = df[df["parameter"].isin(POLLUTANTS)]
            if df.empty:
                continue
            with cur.copy(
                """COPY staging.measurements (payload_id, target_date, location_id, sensor_id,
                       parameter, units, value, ts_utc, day_ist) FROM STDIN"""
            ) as copy:
                for row in df.itertuples(index=False):
                    copy.write_row(
                        (
                            payload_id,
                            day,
                            row.location_id,
                            None if pd.isna(row.sensor_id) else int(row.sensor_id),
                            row.parameter,
                            row.units,
                            None if pd.isna(row.value) else float(row.value),
                            row.ts_utc.to_pydatetime(),
                            row.day_ist,
                        )
                    )
            staged += len(df)
    return {"day": day.isoformat(), "payloads": len(entries), "staged_rows": staged}


def _upsert_locations(cur: psycopg.Cursor, locations: list[dict], day: date) -> None:
    for loc in locations:
        cur.execute(
            """INSERT INTO staging.locations (location_id, name, provider, lat, lon,
                   datetime_first, datetime_last, sensors, discovered_on)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (location_id) DO UPDATE SET
                   name = EXCLUDED.name, provider = EXCLUDED.provider, lat = EXCLUDED.lat,
                   lon = EXCLUDED.lon, datetime_first = EXCLUDED.datetime_first,
                   datetime_last = EXCLUDED.datetime_last, sensors = EXCLUDED.sensors,
                   discovered_on = EXCLUDED.discovered_on
               WHERE staging.locations.discovered_on <= EXCLUDED.discovered_on""",
            (
                loc["location_id"],
                loc["name"],
                loc["provider"],
                loc["lat"],
                loc["lon"],
                loc["datetime_first"],
                loc["datetime_last"],
                Jsonb(loc["sensors"]),
                day,
            ),
        )


def clean_day(conn: psycopg.Connection, day: date, settings: Settings) -> dict:
    """Rebuild core.hourly for one IST day from every staged reading that belongs to it."""
    with conn.transaction(), conn.cursor() as cur:
        # One writer per day: overlapping backfill runs both rebuild the day they share.
        cur.execute("SELECT pg_advisory_xact_lock(4242100, %s)", (day.toordinal() % 2_000_000_000,))
        cur.execute(
            """SELECT location_id, sensor_id, parameter, units, value, ts_utc
                 FROM staging.measurements WHERE day_ist = %s""",
            (day,),
        )
        readings = pd.DataFrame(
            cur.fetchall(),
            columns=["location_id", "sensor_id", "parameter", "units", "value", "ts_utc"],
        )
        cur.execute("SELECT location_id, station_key FROM core.stations")
        stations = pd.DataFrame(cur.fetchall(), columns=["location_id", "station_key"])
        result = clean_readings(readings, stations, settings.ppb_label_means)
        hourly = result.hourly[result.hourly["day_ist"] == day]
        cur.execute("DELETE FROM core.hourly WHERE day_ist = %s", (day,))
        cur.execute("DELETE FROM ops.cleaning_log WHERE day_ist = %s", (day,))
        with cur.copy(
            """COPY core.hourly (station_key, parameter, hour_ist, day_ist, value, n_readings,
                   n_sensors) FROM STDIN"""
        ) as copy:
            for r in hourly.itertuples(index=False):
                copy.write_row(
                    (
                        r.station_key,
                        r.parameter,
                        r.hour_ist.to_pydatetime(),
                        r.day_ist,
                        float(r.value),
                        int(r.n_readings),
                        int(r.n_sensors),
                    )
                )
        cur.executemany(
            "INSERT INTO ops.cleaning_log (day_ist, parameter, reason, n_rows) VALUES (%s, %s, %s, %s)",
            [(day, p, reason, n) for (p, reason), n in sorted(result.dropped.items())],
        )
    kept = int(hourly["n_readings"].sum()) if len(hourly) else 0
    return {
        "day": day.isoformat(),
        "readings_in": len(readings),
        "readings_kept": kept,
        "hourly_rows": len(hourly),
        "dropped": {f"{p}:{r}": n for (p, r), n in sorted(result.dropped.items())},
    }


def clean_days_for_run(conn: psycopg.Connection, day: date, settings: Settings) -> dict:
    """A day's payload can hold the first reading of the next day's 00:00 hour boundary and
    vice versa, so a run rebuilds its own day and the day before."""
    out = {}
    for d in (day - timedelta(days=1), day):
        out[d.isoformat()] = clean_day(conn, d, settings)
    return json.loads(json.dumps(out))
