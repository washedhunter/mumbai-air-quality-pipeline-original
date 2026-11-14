from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "data" / "sample"
SAMPLE_DAYS = ("2026-10-27", "2026-11-09")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Tests never call OpenAQ: no key in the environment and real HTTP transports refuse to send.
    (httpx.MockTransport is unaffected.)"""
    monkeypatch.delenv("OPENAQ_API_KEY", raising=False)

    def refuse(self, request):
        raise RuntimeError(f"network access blocked in tests: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture
def settings_env(monkeypatch, tmp_path):
    """Settings pointing at a temp landing folder and the committed sample."""
    from mumbai_aq.config import Settings

    monkeypatch.setenv("LANDING_DIR", str(tmp_path / "landing"))
    monkeypatch.setenv("SAMPLE_DIR", str(SAMPLE_DIR))
    return Settings


def _test_dsn() -> str | None:
    return os.environ.get("TEST_WAREHOUSE_DSN")


@pytest.fixture(scope="module")
def warehouse_dsn():
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_WAREHOUSE_DSN not set")
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        for schema in ("raw", "staging", "core", "marts", "ops"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    return dsn
