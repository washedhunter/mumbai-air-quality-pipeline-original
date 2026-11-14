"""Daily Mumbai air-quality pipeline.

extract -> validate -> load_raw -> clean -> sql_models -> forecast -> data_quality

One run = one IST calendar day (the run's logical date). Scheduled at 02:00 IST for the previous
day. Every task is idempotent, so re-running a day or a range never duplicates rows. Backfill
runs skip the forecast (fitting ~30 Prophet models per historical day adds nothing); the next
regular run forecasts from the full history.
"""

from __future__ import annotations

from datetime import date, timedelta

import pendulum
from airflow.sdk.exceptions import AirflowSkipException
from airflow.sdk import dag, get_current_context, task
from airflow.timetables.interval import CronDataIntervalTimetable

IST = "Asia/Kolkata"


def _run_day() -> date:
    """The IST day this run processes: its logical date. Scheduled and backfill runs get the
    start of their data interval as logical date; manual runs get whatever date was passed."""
    ctx = get_current_context()
    when = ctx.get("logical_date") or ctx["data_interval_start"]
    return pendulum.instance(when).in_timezone(IST).date()


def _forecast_wanted() -> bool:
    import os

    ctx = get_current_context()
    run_type = str(getattr(ctx["dag_run"], "run_type", "")).lower()
    always = os.environ.get("ALWAYS_FORECAST", "false").lower() in {"1", "true", "yes"}
    return always or "backfill" not in run_type


@dag(
    dag_id="mumbai_air_quality",
    description="OpenAQ -> PostgreSQL -> CPCB AQI SQL models -> Prophet forecast, one IST day per run",
    # Data interval [D 00:00, D+1 00:00) IST; the run starts at 02:00 IST on D+1.
    schedule=CronDataIntervalTimetable("0 0 * * *", timezone=IST),
    start_date=pendulum.datetime(2024, 11, 1, tz=IST),
    catchup=False,  # history is loaded with `airflow backfill create` (see README)
    max_active_runs=4,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": True,
    },
    tags=["air-quality", "mumbai", "openaq"],
)
def mumbai_air_quality():
    @task
    def extract() -> dict:
        from mumbai_aq.pipeline import step_extract

        return step_extract(_run_day())

    @task
    def validate(_: dict) -> dict:
        from mumbai_aq.pipeline import step_validate

        return step_validate(_run_day())

    @task
    def load_raw(_: dict) -> dict:
        from mumbai_aq.pipeline import step_load_raw

        return step_load_raw(_run_day())

    @task
    def clean(_: dict) -> dict:
        from mumbai_aq.pipeline import step_clean

        return step_clean(_run_day())

    @task
    def sql_models(_: dict) -> dict:
        from mumbai_aq.pipeline import step_models

        return step_models(_run_day())

    @task(retries=1)
    def forecast(_: dict) -> dict:
        if not _forecast_wanted():
            raise AirflowSkipException(
                "backfill run: the next regular run forecasts from full history"
            )
        from mumbai_aq.pipeline import step_forecast

        return step_forecast(_run_day())

    @task(trigger_rule="none_failed")
    def data_quality() -> dict:
        from mumbai_aq.pipeline import step_dq

        return step_dq(_run_day(), forecast_expected=_forecast_wanted())

    models_done = sql_models(clean(load_raw(validate(extract()))))
    forecast(models_done) >> data_quality()


mumbai_air_quality()
