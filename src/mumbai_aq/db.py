"""Warehouse connection, schema migration and reference-data seeding."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from . import aqi
from .config import SQL_DIR, Settings

SEASONS = {
    1: ("Winter", 1),
    2: ("Winter", 1),
    3: ("Summer", 2),
    4: ("Summer", 2),
    5: ("Summer", 2),
    6: ("Monsoon", 3),
    7: ("Monsoon", 3),
    8: ("Monsoon", 3),
    9: ("Monsoon", 3),
    10: ("Post-monsoon", 4),
    11: ("Post-monsoon", 4),
    12: ("Post-monsoon", 4),
}


@contextmanager
def connect(settings: Settings) -> Iterator[psycopg.Connection]:
    # autocommit: every write block below uses an explicit conn.transaction()
    conn = psycopg.connect(settings.warehouse_dsn, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def migrate(conn: psycopg.Connection, settings: Settings) -> None:
    """Create schemas/tables/functions and (re)seed reference tables. Idempotent."""
    with conn.transaction():
        # Serialise concurrent runs (backfills start several DAG runs at once).
        conn.execute("SELECT pg_advisory_xact_lock(4242001)")
        for path in sorted((SQL_DIR / "schema").glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
        seed_reference(conn, settings)


def ensure_schema(conn: psycopg.Connection, settings: Settings) -> None:
    """Create the schema if this database has none yet (e.g. `aq local-run` on a fresh DB).

    DAG tasks call this instead of migrate(): re-running the DDL on every task would take table
    locks (CREATE INDEX IF NOT EXISTS takes a SHARE lock even when the index exists) that deadlock
    against other runs' loads. Upgrades are applied by `aq init-db`, which the stack's airflow-init
    service runs on every `docker compose up`."""
    if conn.execute("SELECT to_regclass('raw.payloads')").fetchone()[0] is None:
        migrate(conn, settings)


def seed_reference(conn: psycopg.Connection, settings: Settings) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO core.aqi_breakpoints (pollutant, band, b_lo, b_hi, i_lo, i_hi, category)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (pollutant, band) DO UPDATE SET
                 b_lo = EXCLUDED.b_lo, b_hi = EXCLUDED.b_hi, i_lo = EXCLUDED.i_lo,
                 i_hi = EXCLUDED.i_hi, category = EXCLUDED.category""",
            list(aqi.breakpoint_rows()),
        )
        cur.executemany(
            """INSERT INTO core.seasons (month, season, season_order) VALUES (%s, %s, %s)
               ON CONFLICT (month) DO UPDATE SET season = EXCLUDED.season,
                 season_order = EXCLUDED.season_order""",
            [(m, s, o) for m, (s, o) in SEASONS.items()],
        )
        with settings.stations_file.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        cur.executemany(
            """INSERT INTO core.stations (location_id, station_key, station_name, operator,
                   neighbourhood, zone, in_mumbai, provider, openaq_name, lat, lon)
               VALUES (%(location_id)s, %(station_key)s, %(station_name)s, %(operator)s,
                   %(neighbourhood)s, %(zone)s, %(in_mumbai)s, %(provider)s, %(openaq_name)s,
                   %(lat)s, %(lon)s)
               ON CONFLICT (location_id) DO UPDATE SET
                 station_key = EXCLUDED.station_key, station_name = EXCLUDED.station_name,
                 operator = EXCLUDED.operator, neighbourhood = EXCLUDED.neighbourhood,
                 zone = EXCLUDED.zone, in_mumbai = EXCLUDED.in_mumbai,
                 provider = EXCLUDED.provider, openaq_name = EXCLUDED.openaq_name,
                 lat = EXCLUDED.lat, lon = EXCLUDED.lon""",
            [
                {
                    **r,
                    "location_id": int(r["location_id"]),
                    "in_mumbai": r["in_mumbai"].lower() == "true",
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                }
                for r in rows
            ],
        )
