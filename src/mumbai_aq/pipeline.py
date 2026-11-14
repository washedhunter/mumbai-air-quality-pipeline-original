"""The pipeline's steps as plain functions. The Airflow DAG calls these one per task; `aq local-run`
calls them in sequence without Airflow (used by CI and for debugging)."""

from __future__ import annotations

from datetime import date, timedelta

from . import db, dq, forecast, load, models, validate
from .config import Settings, get_settings
from .extract import extract_day


def step_extract(day: date, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    return extract_day(day, settings)


def step_validate(day: date, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    return validate.validate_day(settings.landing_dir, day)


def step_load_raw(day: date, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    with db.connect(settings) as conn:
        db.migrate(conn, settings)
        return load.load_raw(conn, settings.landing_dir, day)


def step_clean(day: date, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    with db.connect(settings) as conn:
        return load.clean_days_for_run(conn, day, settings)


def step_models(day: date, settings: Settings | None = None) -> dict:
    """Daily models are rebuilt for the day before through the day after the run day: the day
    before because cleaning touched it, the day after because its 8-hour CO/O3 windows start
    in this run's day (so load order during a backfill can't change the result)."""
    settings = settings or get_settings()
    with db.connect(settings) as conn:
        return models.run_models(conn, day - timedelta(days=1), day + timedelta(days=1))


def step_forecast(day: date, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    with db.connect(settings) as conn:
        return forecast.run_forecast(conn, settings, day)


def step_dq(day: date, forecast_expected: bool, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    with db.connect(settings) as conn:
        return dq.run_checks(conn, day, forecast_expected)


def run_day(day: date, settings: Settings | None = None, with_forecast: bool = True) -> dict:
    settings = settings or get_settings()
    out = {
        "extract": step_extract(day, settings),
        "validate": step_validate(day, settings),
        "load_raw": step_load_raw(day, settings),
        "clean": step_clean(day, settings),
        "models": step_models(day, settings),
    }
    if with_forecast:
        out["forecast"] = step_forecast(day, settings)
    out["dq"] = step_dq(day, with_forecast, settings)
    return out
