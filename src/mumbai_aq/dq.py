"""Data-quality checks run at the end of every DAG run. Results go to ops.dq_results;
any failed 'error' check fails the task (and so the run)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import psycopg


@dataclass(frozen=True)
class Check:
    name: str
    severity: str  # error | warn
    sql: str  # returns one row: (passed boolean, observed text)
    detail: str


def checks(run_date: date, forecast_expected: bool, min_city_days: int = 60) -> list[Check]:
    """Every check is scoped to the run day, and each one fails on a real problem."""
    d = run_date.isoformat()
    pollutants = "('pm25','pm10','no2','so2','o3','co','nh3')"
    out = [
        Check(
            "raw_requests_recorded",
            "error",
            f"SELECT count(*) > 0, count(*)::text FROM raw.payloads WHERE target_date = '{d}'",
            "Every request made for the day (including 404s) was stored in raw.payloads.",
        ),
        Check(
            "staging_not_duplicated",
            "error",
            f"""SELECT count(*) = 0, count(*)::text FROM (
                   SELECT location_id, sensor_id, parameter, ts_utc
                     FROM staging.measurements WHERE target_date = '{d}'
                    GROUP BY 1, 2, 3, 4 HAVING count(*) > 1) x""",
            "No reading appears twice for the day (idempotent loads).",
        ),
        Check(
            "hourly_consistent_with_staging",
            "error",
            f"""SELECT NOT (s.n > 0 AND h.n = 0), s.n || ' readings -> ' || h.n || ' hourly rows'
                  FROM (SELECT count(*) AS n FROM staging.measurements
                         WHERE day_ist = '{d}' AND parameter IN {pollutants}) s,
                       (SELECT count(*) AS n FROM core.hourly WHERE day_ist = '{d}') h""",
            "If the day has pollutant readings, cleaning kept at least some of them.",
        ),
        Check(
            "marts_consistent_with_hourly",
            "error",
            f"""SELECT NOT (h.n > 0 AND m.n = 0), h.n || ' hourly rows -> ' || m.n || ' daily rows'
                  FROM (SELECT count(*) AS n FROM core.hourly WHERE day_ist = '{d}') h,
                       (SELECT count(*) AS n FROM marts.daily_pollutant WHERE day = '{d}') m""",
            "If the day has hourly data, the daily SQL models include it.",
        ),
        Check(
            "aqi_rule_respected",
            "error",
            f"""SELECT count(*) = 0, count(*)::text FROM marts.daily_station_aqi
                 WHERE day = '{d}' AND aqi IS NOT NULL
                   AND (n_pollutants < 3 OR NOT has_pm OR aqi NOT BETWEEN 0 AND 500)""",
            "No AQI was published without 3 pollutants incl. PM2.5 or PM10 (guards the SQL model).",
        ),
        Check(
            "city_aqi_consistent",
            "error",
            f"""SELECT (s.n >= 3) = (c.n = 1), s.n || ' Mumbai stations with AQI, city rows ' || c.n
                  FROM (SELECT count(*) AS n FROM marts.daily_station_aqi a
                          JOIN marts.dim_station st USING (station_key)
                         WHERE a.day = '{d}' AND a.aqi IS NOT NULL AND st.in_mumbai) s,
                       (SELECT count(*) AS n FROM marts.city_daily_aqi WHERE day = '{d}') c""",
            "The city AQI exists exactly when at least 3 Mumbai stations have an AQI.",
        ),
        Check(
            "source_has_data",
            "warn",
            f"SELECT count(*) > 0, count(*)::text FROM staging.measurements WHERE target_date = '{d}'",
            "The source returned readings for the day (a gap in OpenAQ itself shows up here).",
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
            "stations_vs_trailing_week",
            "warn",
            f"""WITH t AS (SELECT count(*) FILTER (WHERE day = '{d}') AS today,
                              count(*) FILTER (WHERE day < '{d}') / 7.0 AS trailing_avg
                         FROM marts.daily_station_aqi
                        WHERE aqi IS NOT NULL AND day BETWEEN '{d}'::date - 7 AND '{d}')
               SELECT trailing_avg = 0 OR today >= 0.7 * trailing_avg,
                      today || ' today vs ' || round(trailing_avg, 1) || ' per day last week' FROM t""",
            "Stations with an AQI did not drop by more than 30% against the previous 7 days.",
        ),
        Check(
            "day_complete",
            "warn",
            f"""SELECT count(*) = 0 OR max(extract(hour FROM hour_ist)) >= 22,
                       coalesce(max(hour_ist)::text, 'no data')
                  FROM core.hourly WHERE day_ist = '{d}'""",
            "The day has data up to at least 22:00 IST (catches a run that beat the API's lag).",
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
                        WHERE day_ist = '{d}' AND parameter IN {pollutants}), 1),
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
                           OR (SELECT count(*) FROM marts.city_daily_aqi WHERE day <= '{d}')
                              < {int(min_city_days)},
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
