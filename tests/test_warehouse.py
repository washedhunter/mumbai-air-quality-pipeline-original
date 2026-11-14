"""Integration tests against a real PostgreSQL (CI service / local container).

Runs the whole pipeline on the committed sample (real OpenAQ data, 6 stations, 14 days),
then checks idempotency, SQL-vs-Python AQI parity, the SQL models and the forecast.
Set TEST_WAREHOUSE_DSN to run; skipped otherwise.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pandas as pd
import psycopg
import pytest

from mumbai_aq import aqi, db, models, pipeline
from mumbai_aq.config import Settings

pytestmark = pytest.mark.db

FIRST, LAST = date(2026, 10, 27), date(2026, 11, 9)
TABLES = (
    "raw.payloads",
    "staging.measurements",
    "core.hourly",
    "marts.daily_pollutant",
    "marts.daily_station_aqi",
    "marts.city_daily_aqi",
    "marts.weekly_trends",
    "marts.seasonal_patterns",
    "marts.neighbourhood_pollutant",
    "marts.dominant_pollutant",
    "marts.station_coverage",
    "marts.forecasts",
)


def _fingerprint(conn) -> dict[str, tuple]:
    """Row count + order-independent checksum of the business columns of every table."""
    cols = {
        "raw.payloads": "source, endpoint, location_id, sensor_id, target_date, sha256",
        "staging.measurements": "target_date, location_id, sensor_id, parameter, value, ts_utc",
        "core.hourly": "station_key, parameter, hour_ist, value, n_readings",
        "marts.forecasts": "run_date, level, station_key, target_date, aqi_forecast, aqi_lower, aqi_upper",
    }
    out = {}
    with conn.cursor() as cur:
        for t in TABLES:
            c = cols.get(t, "*")
            cur.execute(
                f"SELECT count(*), coalesce(sum(hashtext(x::text)::bigint), 0) FROM (SELECT {c} FROM {t}) x"
            )
            out[t] = cur.fetchone()
    return out


@pytest.fixture(scope="module")
def loaded(warehouse_dsn, tmp_path_factory):
    from tests.conftest import SAMPLE_DIR

    s = replace(
        Settings(),
        warehouse_dsn=warehouse_dsn,
        extract_source="sample",
        sample_dir=SAMPLE_DIR,
        landing_dir=tmp_path_factory.mktemp("landing"),
        openaq_api_key="",
        forecast_min_days_city=10,
        forecast_min_days_station=10,
        backtest_weeks=0,
        backtest_station_origins=0,
    )
    # Load days out of order on purpose: the result must not depend on load order.
    days = [FIRST + timedelta(days=i) for i in range((LAST - FIRST).days + 1)]
    order = days[1::2] + days[0::2]
    for d in order:
        pipeline.run_day(d, s, with_forecast=False)
    pipeline.run_day(LAST, s, with_forecast=True)
    return s


def test_everything_populated(loaded):
    with db.connect(loaded) as conn:
        fp = _fingerprint(conn)
    for table, (n, _) in fp.items():
        assert n > 0, f"{table} is empty"
    assert fp["raw.payloads"][0] == 14 * 6


def test_rerun_is_idempotent(loaded):
    with db.connect(loaded) as conn:
        before = _fingerprint(conn)
    pipeline.run_day(date(2026, 11, 3), loaded, with_forecast=False)
    pipeline.run_day(LAST, loaded, with_forecast=True)
    with db.connect(loaded) as conn:
        after = _fingerprint(conn)
    assert after == before


def test_load_order_does_not_matter(loaded):
    """Rebuild every day again in calendar order and compare with the out-of-order build."""
    with db.connect(loaded) as conn:
        before = _fingerprint(conn)
    d = FIRST
    while d <= LAST:
        pipeline.run_day(d, loaded, with_forecast=d == LAST)
        d += timedelta(days=1)
    with db.connect(loaded) as conn:
        assert _fingerprint(conn) == before


def test_sql_subindex_matches_python(loaded):
    grid = []
    for pollutant, bands in aqi.BREAKPOINTS.items():
        top = bands[-1][1]
        steps = [top * i / 400 for i in range(0, 461)]  # 0 .. 115% of the top breakpoint
        grid += [(pollutant, round(x, 3)) for x in steps]
    with db.connect(loaded) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT g.p, g.c, core.aqi_subindex(g.p, g.c)
                 FROM unnest(%s::text[], %s::float8[]) AS g(p, c)""",
            ([p for p, _ in grid], [c for _, c in grid]),
        )
        rows = cur.fetchall()
    mismatches = [
        (p, c, sql, aqi.sub_index(p, c))
        for p, c, sql in rows
        if sql is None or abs(sql - aqi.sub_index(p, c)) > 1e-9
    ]
    assert not mismatches, mismatches[:5]


def test_sql_daily_aqi_matches_python(loaded):
    """Recompute every station-day AQI in Python from core.hourly and compare with the SQL model."""
    with db.connect(loaded) as conn, conn.cursor() as cur:
        cur.execute("SELECT station_key, parameter, hour_ist, value FROM core.hourly")
        hourly = pd.DataFrame(
            cur.fetchall(), columns=["station_key", "parameter", "hour_ist", "value"]
        )
        cur.execute("SELECT station_key, day, aqi, dominant_pollutant FROM marts.daily_station_aqi")
        sql = {(k, d): (a, p) for k, d, a, p in cur.fetchall()}
    hourly["hour_ist"] = pd.to_datetime(hourly["hour_ist"])
    checked = 0
    for (key, day), _ in sql.items():
        start = pd.Timestamp(day)
        window = hourly[
            (hourly["station_key"] == key)
            & (hourly["hour_ist"] >= start - pd.Timedelta(hours=7))
            & (hourly["hour_ist"] < start + pd.Timedelta(days=1))
        ]
        series = {
            p: g.set_index("hour_ist")["value"].sort_index() for p, g in window.groupby("parameter")
        }
        # daily_concentration takes the day from the last timestamp: keep only series with data on the day
        series = {p: s for p, s in series.items() if (s.index >= start).any()}
        res = aqi.day_aqi(series)
        assert (res.aqi, res.dominant) == sql[(key, day)], (key, day, res, sql[(key, day)])
        checked += 1
    assert checked > 50


def test_models_are_all_materialised(loaded):
    names = {m.name for m in models.discover()}
    assert {
        "10_daily_pollutant",
        "20_daily_station_aqi",
        "40_city_daily_aqi",
        "50_weekly_trends",
        "60_seasonal_patterns",
        "70_neighbourhood_pollutant",
        "80_dominant_pollutant",
    } <= names
    with db.connect(loaded) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT season FROM marts.seasonal_patterns")
        assert {r[0] for r in cur.fetchall()} == {"Post-monsoon"}  # the sample is Oct-Nov
        cur.execute("SELECT count(*) FROM marts.city_daily_aqi WHERE n_stations < 1 OR aqi IS NULL")
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM marts.neighbourhood_pollutant WHERE rank_in_month < 1 OR ratio_to_average <= 0"
        )
        assert cur.fetchone()[0] == 0


def test_forecast_and_dq(loaded):
    with db.connect(loaded) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT count(*), min(target_date), max(target_date), bool_and(aqi_lower <= aqi_forecast
                      AND aqi_forecast <= aqi_upper)
                 FROM marts.forecasts WHERE level = 'city' AND run_date = %s""",
            (LAST,),
        )
        n, first, last, ordered = cur.fetchone()
        assert (n, first, last, ordered) == (
            7,
            LAST + timedelta(days=1),
            LAST + timedelta(days=7),
            True,
        )
        cur.execute(
            "SELECT check_name FROM ops.dq_results WHERE run_date = %s AND severity = 'error' AND NOT passed",
            (LAST,),
        )
        assert cur.fetchall() == []


def test_dq_fails_loudly_on_bad_data(loaded):
    from mumbai_aq import dq

    with db.connect(loaded) as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("DELETE FROM core.hourly WHERE day_ist = %s", (LAST,))
        with pytest.raises(dq.DataQualityError, match="hourly_consistent_with_staging"):
            dq.run_checks(conn, LAST, forecast_expected=True)
    pipeline.run_day(LAST, loaded, with_forecast=True)  # repair


def test_dq_checks_all_pass_for_the_last_day(loaded):
    with db.connect(loaded) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT check_name, severity, passed FROM ops.dq_results WHERE run_date = %s", (LAST,)
        )
        rows = cur.fetchall()
    assert len(rows) == 13
    assert all(passed for _, sev, passed in rows if sev == "error")


def test_migrate_is_repeatable(loaded):
    with psycopg.connect(loaded.warehouse_dsn, autocommit=True) as conn:
        db.migrate(conn, loaded)
        db.migrate(conn, loaded)
        assert conn.execute("SELECT count(*) FROM core.aqi_breakpoints").fetchone()[0] == 42
        assert conn.execute("SELECT count(*) FROM core.stations").fetchone()[0] == 44


def test_powerbi_guide_references_exist(loaded):
    """Every table[column] and measure the Power BI guide names exists in the warehouse."""
    import importlib.util

    from tests.conftest import ROOT

    spec = importlib.util.spec_from_file_location(
        "check_guide", ROOT / "scripts" / "check_powerbi_guide.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.check(loaded.warehouse_dsn) == []


def test_forecast_check_tolerates_short_history(loaded):
    """With less city history than the forecast minimum, a missing forecast is not an error."""
    from mumbai_aq import dq

    day = LAST + timedelta(days=30)  # no forecast for this run date
    with db.connect(loaded) as conn, conn.cursor() as cur:
        strict = next(
            c for c in dq.checks(day, True, min_city_days=5) if c.name == "forecast_written"
        )
        lenient = next(
            c for c in dq.checks(day, True, min_city_days=10_000) if c.name == "forecast_written"
        )
        cur.execute(strict.sql)
        assert cur.fetchone()[0] is False
        cur.execute(lenient.sql)
        assert cur.fetchone()[0] is True


def test_reports_with_a_backtest(loaded, tmp_path):
    """aq report must work once a backtest exists (it crashed on this before)."""
    from mumbai_aq.reporting import write_reports

    run = LAST + timedelta(days=100)  # a run date no other test uses
    rows = [
        (
            run,
            "city",
            "mumbai",
            LAST - timedelta(days=7),
            LAST - timedelta(days=7 - h),
            h,
            m,
            100.0,
            100.0 + e,
        )
        for h in range(1, 8)
        for m, e in (("prophet", 5.0), ("last_value", 9.0), ("seasonal_naive_7d", 12.0))
    ]
    with db.connect(loaded) as conn, conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO marts.forecast_backtest VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", rows
        )
    try:
        paths = write_reports(loaded, tmp_path)
        text = (tmp_path / "backtest.md").read_text(encoding="utf-8")
        assert "| city | 9.0 | 5.0 | 12.0 |" in text
        assert {p.name for p in paths} == {"backtest.md", "coverage.md", "summary.json"}
        with db.connect(loaded) as conn:
            pooled = conn.execute(
                "SELECT sum(mae * n) / sum(n) FROM marts.backtest_summary WHERE model = 'prophet'"
            ).fetchone()[0]
        assert pooled == pytest.approx(5.0)
    finally:
        with db.connect(loaded) as conn:
            conn.execute("DELETE FROM marts.forecast_backtest WHERE run_date = %s", (run,))


def test_concurrent_loads_do_not_deadlock(loaded):
    """Several runs loading different days at once (a backfill) must not deadlock."""
    from concurrent.futures import ThreadPoolExecutor

    days = [FIRST + timedelta(days=i) for i in range(6)]
    for d in days:
        pipeline.step_extract(d, loaded)
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda d: pipeline.step_load_raw(d, loaded), days))
    assert all(r["staged_rows"] > 0 for r in results)
