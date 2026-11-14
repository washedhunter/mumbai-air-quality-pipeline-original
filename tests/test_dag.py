"""DAG import test. Needs Airflow, so it runs inside the Airflow image (CI docker job):
docker compose run --rm --entrypoint python pipeline -m pytest tests/test_dag.py"""

import itertools
from pathlib import Path

import pytest

pytest.importorskip("airflow")
pytestmark = pytest.mark.airflow

_REPO_DAGS = Path(__file__).resolve().parent.parent / "dags"
DAGS = _REPO_DAGS if _REPO_DAGS.is_dir() else Path("/opt/airflow/dags")  # inside the image


@pytest.fixture(scope="module")
def dagbag():
    from airflow.dag_processing.dagbag import DagBag

    return DagBag(dag_folder=str(DAGS))


def test_no_import_errors(dagbag):
    assert dagbag.import_errors == {}
    assert set(dagbag.dag_ids) == {"mumbai_air_quality"}


def test_dag_structure(dagbag):
    dag = dagbag.dags["mumbai_air_quality"]  # no metadata DB needed
    assert dag is not None
    order = ["extract", "validate", "load_raw", "clean", "sql_models", "forecast", "data_quality"]
    assert set(dag.task_ids) == set(order)
    for upstream, downstream in itertools.pairwise(order):
        assert downstream in dag.get_task(upstream).downstream_task_ids
    assert dag.get_task("data_quality").trigger_rule == "none_failed"
    assert dag.catchup is False
    assert dag.max_active_runs == 4
    assert all(t.retries >= 1 for t in dag.tasks)


def test_schedule_is_daily_at_two_ist(dagbag):
    """Logical date = 02:00 IST on the day processed; it must match the CLI's trigger dates."""
    from mumbai_aq.cli import RUN_HOUR_IST, _logical

    dag = dagbag.dags["mumbai_air_quality"]
    assert dag.timetable.summary.startswith("0 2 * * *") or "0 2 * * *" in str(
        dag.timetable.summary
    )
    assert dag.start_date.hour == RUN_HOUR_IST
    assert _logical(__import__("datetime").date(2026, 11, 13)) == "2026-11-13T02:00:00+05:30"
