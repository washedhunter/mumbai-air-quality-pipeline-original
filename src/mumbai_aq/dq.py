"""Data-quality checks run at the end of every DAG run. Results go to ops.dq_results;
any failed 'error' check fails the task (and so the run)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import psycopg

from .clean import MAX_PLAUSIBLE


@dataclass(frozen=True)
class Check:
    name: str
    severity: str  # error | warn
    sql: str  # returns one row: (passed boolean, observed text)
    detail: str


def checks(run_date: date, forecast_expected: bool, min_city_days: int = 60) -> list[Check]:
    limits = " OR ".join(f"(parameter = '{p}' AND value >= {v})" for p, v in MAX_PLAUSIBLE.items())
    d = run_date.isoformat()
    out = [
        Check(
            "raw_payloads_loaded",
            "error",
            f"SELECT count(*) > 0, count(*)::text FROM raw.payloads WHERE target_date = '{d}' AND http_status = 200",
            "At least one successful raw payload was stored for the run day.",
        ),
        Check(
            "staging_rows_for_day",
            "error",
            f"SELECT count(*) > 0, count(*)::text FROM staging.measurements WHERE target_date = '{d}'",
            "The run day's payloads produced readings.",
        ),
        Check(
            "hourly_rows_for_day",
            "error",
            f"SELECT count(*) > 0, count(*)::text FROM core.hourly WHERE day_ist = '{d}'",
            "Cleaning kept hourly values for the run day.",
        ),
        Check(
            "hourly_values_plausible",
            "error",
            f"SELECT count(*) = 0, count(*)::text FROM core.hourly WHERE value < 0 OR {limits}",
            "No negative or implausible hourly values survived cleaning.",
        ),
        Check(
            "staging_not_duplicated",
            "error",
            """SELECT count(*) = 0, count(*)::text FROM (
                   SELECT location_id, sensor_id, parameter, ts_utc, target_date
                     FROM staging.measurements GROUP BY 1, 2, 3, 4, 5 HAVING count(*) > 1) d""",
            "No reading appears twice for the same payload day (idempotent loads).",
        ),
        Check(
            "aqi_in_range",
            "error",
            "SELECT count(*) = 0, count(*)::text FROM marts.daily_station_aqi WHERE aqi < 0 OR aqi > 500",
            "Every station AQI is within 0-500.",
        ),
        Check(
            "aqi_rule_respected",
            "error",
            """SELECT count(*) = 0, count(*)::text FROM marts.daily_station_aqi
                WHERE aqi IS NOT NULL AND (n_pollutants < 3 OR NOT has_pm)""",
            "No AQI was published without 3 pollutants including PM2.5 or PM10.",
        ),
        Check(
            "marts_cover_run_day",
            "error",
            f"SELECT count(*) > 0, count(*)::text FROM marts.daily_pollutant WHERE day = '{d}'",
            "The SQL models include the run day.",
        ),
        Check(
            "station_coverage_for_day",
            "warn",
            f"""WITH active AS (SELECT count(*) AS n FROM marts.station_coverage
                                  WHERE '{d}' BETWEEN first_day AND last_day),
                     today AS (SELECT count(DISTINCT station_key) AS n FROM marts.daily_station_aqi
                                WHERE day = '{d}' AND aqi IS NOT NULL)
               SELECT today.n >= 0.5 * active.n, today.n || ' of ' || active.n
                 FROM active, today""",
            "At least half of the stations active around the run day have an AQI for it.",
        ),
        Check(
            "unmapped_locations",
            "warn",
            "SELECT count(*) = 0, coalesce(string_agg(station_key, ','), '') FROM marts.dim_station WHERE zone = 'Unmapped'",
            "Every OpenAQ location is mapped to a neighbourhood in config/stations.csv.",
        ),
        Check(
            "cleaning_drop_rate",
            "warn",
            f"""SELECT coalesce(sum(n_rows), 0) <= 0.3 * greatest((SELECT count(*) FROM staging.measurements
                        WHERE day_ist = '{d}' AND parameter IN ('pm25','pm10','no2','so2','o3','co','nh3')), 1),
                       coalesce(sum(n_rows), 0)::text
                  FROM ops.cleaning_log WHERE day_ist = '{d}'""",
            "Cleaning dropped at most 30% of the day's pollutant readings.",
        ),
    ]
    if forecast_expected:
        out.append(
            Check(
                "forecast_written",
                "error",
                f"""SELECT count(*) > 0
                           OR (SELECT count(*) FROM marts.city_daily_aqi
                                WHERE n_stations >= 3 AND day <= '{d}') < {int(min_city_days)},
                           count(*)::text
                      FROM marts.forecasts WHERE run_date = '{d}' AND level = 'city'""",
                "The city 7-day forecast was written (unless the city series is still too short).",
            )
        )
    return out


class DataQualityError(RuntimeError):
    pass


def run_checks(
    conn: psycopg.Connection, run_date: date, forecast_expected: bool, min_city_days: int = 60
) -> dict:
    results = []
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM ops.dq_results WHERE run_date = %s", (run_date,))
        for c in checks(run_date, forecast_expected, min_city_days):
            cur.execute(c.sql)
            passed, observed = cur.fetchone()
            passed = bool(passed)
            cur.execute(
                """INSERT INTO ops.dq_results (run_date, check_name, severity, passed, observed, detail)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (run_date, c.name, c.severity, passed, observed, c.detail),
            )
            results.append(
                {"check": c.name, "severity": c.severity, "passed": passed, "observed": observed}
            )
    failed = [r for r in results if r["severity"] == "error" and not r["passed"]]
    if failed:
        raise DataQualityError(f"{len(failed)} data-quality check(s) failed: {failed}")
    return {"run_date": run_date.isoformat(), "results": results}
