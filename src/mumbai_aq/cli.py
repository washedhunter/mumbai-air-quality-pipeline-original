"""`aq` command line.

Inside the Airflow image (docker compose run --rm pipeline <command>):
  run-day DAY            trigger (or re-run) the DAG for one IST day and wait for it
  backfill FROM TO       backfill a range of days through Airflow and wait for every run
  status FROM TO         show run states for a range
Anywhere with a warehouse:
  init-db                create schemas/tables and seed reference data
  local-run DAY [--to]   run the pipeline steps without Airflow (CI, debugging)
  report                 write reports/backtest.md, reports/coverage.md, reports/summary.json
  make-sample            refresh the committed sample (data/sample) from the public archive
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import typer

from .config import IST, get_settings

app = typer.Typer(add_completion=False, no_args_is_help=True)
DAG_ID = "mumbai_air_quality"
SAMPLE_LOCATIONS = {6927, 6945, 3409482, 3409323, 3409483, 3409510}
TERMINAL = {"success", "failed"}


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


RUN_HOUR_IST = 2  # the DAG's cron hour: a day's run has logical date <day> 02:00 IST


def _logical(day: date) -> str:
    return datetime.combine(day, datetime.min.time(), IST).replace(hour=RUN_HOUR_IST).isoformat()


# ---------------------------------------------------------------- Airflow wrappers
def _airflow(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["airflow", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        typer.echo(proc.stdout + proc.stderr, err=True)
        raise typer.Exit(proc.returncode)
    return proc


def _runs() -> dict[date, dict]:
    proc = _airflow("dags", "list-runs", DAG_ID, "--output", "json")
    text = proc.stdout.strip()
    start = text.find("[")
    rows = json.loads(text[start:]) if start >= 0 else []
    out: dict[date, dict] = {}
    for r in rows:
        if not r.get("logical_date"):
            continue
        day = datetime.fromisoformat(r["logical_date"]).astimezone(IST).date()
        # Keep the newest run per day.
        if day not in out or str(r.get("start_date") or "") >= str(
            out[day].get("start_date") or ""
        ):
            out[day] = r
    return out


def _wait(days: list[date], timeout_min: int, poll_s: int = 15) -> dict[date, str]:
    deadline = time.time() + timeout_min * 60
    wanted = set(days)
    last_report = ""
    while True:
        runs = _runs()
        states = {d: (runs[d]["state"] if d in runs else "missing") for d in wanted}
        counts: dict[str, int] = {}
        for s in states.values():
            counts[s] = counts.get(s, 0) + 1
        report = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        if report != last_report:
            typer.echo(f"[{datetime.now(IST):%H:%M:%S}] {report}")
            last_report = report
        if all(s in TERMINAL for s in states.values()):
            return states
        if time.time() > deadline:
            typer.echo("timed out waiting for runs", err=True)
            return states
        time.sleep(poll_s)


FINGERPRINT_COLUMNS = {
    # business columns only: fetch/load timestamps and surrogate ids legitimately change on re-runs
    "raw.payloads": "source, endpoint, location_id, sensor_id, target_date, http_status, sha256",
    "staging.measurements": "target_date, location_id, sensor_id, parameter, units, value, ts_utc",
    "core.hourly": "station_key, parameter, hour_ist, value, n_readings, n_sensors",
    "marts.daily_pollutant": "*",
    "marts.daily_station_aqi": "*",
    "marts.city_daily_aqi": "*",
    "marts.weekly_trends": "*",
    "marts.seasonal_patterns": "*",
    "marts.neighbourhood_pollutant": "*",
    "marts.dominant_pollutant": "*",
    "marts.station_coverage": "*",
    "marts.forecasts": "run_date, level, station_key, target_date, aqi_forecast, aqi_lower, aqi_upper",
    "marts.forecast_backtest": "*",
}


@app.command()
def fingerprint() -> None:
    """Row count + order-independent checksum per table. Run before and after a re-run: identical
    output means the re-run changed nothing."""
    from . import db

    with db.connect(get_settings()) as conn, conn.cursor() as cur:
        for table, cols in FINGERPRINT_COLUMNS.items():
            cur.execute("SELECT to_regclass(%s)", (table,))
            if cur.fetchone()[0] is None:
                typer.echo(f"{table:<32} {'(not created yet)':>12}")
                continue
            cur.execute(
                f"SELECT count(*), coalesce(sum(hashtextextended(x::text, 0)), 0) "
                f"FROM (SELECT {cols} FROM {table}) x"
            )
            n, checksum = cur.fetchone()
            typer.echo(f"{table:<32} {n:>12,}  {checksum}")


def _print_counts() -> None:
    from . import db

    settings = get_settings()
    with db.connect(settings) as conn, conn.cursor() as cur:
        for table in (
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
            "marts.forecasts",
            "marts.forecast_backtest",
        ):
            cur.execute(f"SELECT count(*) FROM {table}")
            typer.echo(f"  {table:<32} {cur.fetchone()[0]:>10,}")


@app.command("run-day")
def run_day_cmd(
    day: str = typer.Argument(..., help="IST day, YYYY-MM-DD"),
    timeout_min: int = typer.Option(60, help="Give up waiting after this many minutes"),
) -> None:
    """Run the DAG for one day (re-runs it if a run already exists) and wait."""
    d = _parse_day(day)
    if d in _runs():
        typer.echo(f"Re-running the existing run for {d} (clearing all its tasks)")
        _airflow(
            "tasks", "clear", DAG_ID,
            "--start-date", _logical(d), "--end-date", _logical(d), "--yes",
        )  # fmt: skip
    else:
        # Trigger before unpausing: unpausing also starts the daily schedule, whose first run
        # (yesterday) would otherwise race with a manual trigger for the same day.
        _airflow(
            "dags", "trigger", DAG_ID, "--logical-date", _logical(d), "--run-id", f"manual__{d}"
        )
    _airflow("dags", "unpause", DAG_ID)
    states = _wait([d], timeout_min)
    typer.echo(f"{d}: {states[d]}")
    _print_counts()
    raise typer.Exit(0 if states[d] == "success" else 1)


@app.command()
def backfill(
    start: str = typer.Argument(..., help="first IST day, YYYY-MM-DD"),
    end: str = typer.Argument(..., help="last IST day, YYYY-MM-DD"),
    max_active_runs: int = typer.Option(4, help="Runs executing at the same time"),
    rerun: bool = typer.Option(False, help="Also re-run days that already succeeded"),
    timeout_min: int = typer.Option(24 * 60, help="Give up waiting after this many minutes"),
) -> None:
    """Backfill a range through Airflow and wait for all runs to finish."""
    d0, d1 = _parse_day(start), _parse_day(end)
    _airflow("dags", "unpause", DAG_ID)
    _airflow(
        "backfill",
        "create",
        "--dag-id",
        DAG_ID,
        "--from-date",
        _logical(d0),
        "--to-date",
        _logical(d1),
        "--max-active-runs",
        str(max_active_runs),
        "--reprocess-behavior",
        "completed" if rerun else "failed",
    )
    states = _wait(_days(d0, d1), timeout_min, poll_s=30)
    bad = sorted(d for d, s in states.items() if s != "success")
    typer.echo(f"{len(states) - len(bad)}/{len(states)} runs succeeded")
    if bad:
        typer.echo("not successful: " + ", ".join(f"{d} ({states[d]})" for d in bad[:50]))
    _print_counts()
    raise typer.Exit(0 if not bad else 1)


@app.command()
def status(start: str, end: str) -> None:
    """Run states for a range of days."""
    runs = _runs()
    for d in _days(_parse_day(start), _parse_day(end)):
        r = runs.get(d)
        typer.echo(f"{d}  {r['state'] if r else 'missing':<8} {r.get('run_id', '') if r else ''}")


# ---------------------------------------------------------------- warehouse commands
@app.command("init-db")
def init_db() -> None:
    """Create schemas, tables and functions; seed stations, AQI breakpoints and seasons."""
    from . import db

    settings = get_settings()
    with db.connect(settings) as conn:
        db.migrate(conn, settings)
    typer.echo("warehouse ready")


@app.command("local-run")
def local_run(
    day: str = typer.Argument(..., help="first IST day"),
    to: str | None = typer.Option(None, help="last IST day (default: same as DAY)"),
    forecast: bool = typer.Option(True, help="Forecast after the last day"),
) -> None:
    """Run the pipeline steps directly (no Airflow), one day at a time."""
    from .pipeline import run_day

    settings = get_settings()
    d0 = _parse_day(day)
    d1 = _parse_day(to) if to else d0
    for d in _days(d0, d1):
        out = run_day(d, settings, with_forecast=forecast and d == d1)
        failed = [r["check"] for r in out["dq"]["results"] if not r["passed"]]
        typer.echo(
            f"{d}: staged {out['load_raw']['staged_rows']:,} readings, "
            f"{out['clean'][d.isoformat()]['hourly_rows']:,} hourly rows"
            + (f", forecast rows {out['forecast']['forecast_rows']}" if "forecast" in out else "")
            + (f", warnings: {failed}" if failed else "")
        )
    _print_counts()


@app.command()
def report(
    out_dir: Path = typer.Option(Path("reports"), help="Where to write the reports"),
    img_dir: Path | None = typer.Option(
        None, help="Also draw the README charts here (e.g. docs/img)"
    ),
) -> None:
    """Backtest MAE, station coverage and table counts from the warehouse, as Markdown/JSON."""
    from .reporting import write_reports

    settings = get_settings()
    paths = write_reports(settings, out_dir)
    if img_dir is not None:
        from .plots import write_plots

        paths += write_plots(settings, img_dir)
    for p in paths:
        typer.echo(f"wrote {p}")


@app.command("make-sample")
def make_sample(
    start: str = typer.Argument("2026-10-27"),
    end: str = typer.Argument("2026-11-09"),
) -> None:
    """Download a small real subset (6 Mumbai stations) from the public archive into data/sample."""
    from .extract import extract_day

    settings = get_settings()
    sample_settings = replace(settings, extract_source="archive", openaq_api_key="")
    landing = settings.sample_dir / "landing"
    for d in _days(_parse_day(start), _parse_day(end)):
        res = extract_day(d, sample_settings, landing_root=landing, location_ids=SAMPLE_LOCATIONS)
        typer.echo(f"{d}: {res['payloads']} payloads")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
