"""Run the SQL models in sql/models/ in file-name order.

Each file starts with `-- materialized: incremental | table | view`.
- incremental: rebuilds only the days the run touched (%(day_from)s .. %(day_to)s)
- table / view: rebuilt from scratch (cheap: they read the daily tables, not the hourly one)

All models run in one transaction behind an advisory lock, so concurrent backfill runs queue up
instead of dropping each other's tables, and Power BI never sees a half-built set.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import psycopg

from .config import SQL_DIR

MATERIALIZED = re.compile(r"^--\s*materialized:\s*(incremental|table|view)\s*$", re.MULTILINE)
MODELS_LOCK = 4242002


@dataclass(frozen=True)
class Model:
    name: str
    path: Path
    materialized: str

    def statements(self) -> list[str]:
        text = self.path.read_text(encoding="utf-8")
        # Model files contain no semicolons except statement ends (no function bodies).
        body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
        return [s.strip() for s in body.split(";") if s.strip()]


def discover(sql_dir: Path = SQL_DIR) -> list[Model]:
    models = []
    for path in sorted((sql_dir / "models").glob("*.sql")):
        match = MATERIALIZED.search(path.read_text(encoding="utf-8"))
        if not match:
            raise ValueError(f"{path.name}: missing '-- materialized: ...' header")
        models.append(Model(path.stem, path, match.group(1)))
    return models


def run_models(conn: psycopg.Connection, day_from: date, day_to: date) -> dict:
    timings: dict[str, float] = {}
    params = {"day_from": day_from, "day_to": day_to}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (MODELS_LOCK,))
        for model in discover():
            start = time.perf_counter()
            for stmt in model.statements():
                if model.materialized == "incremental":
                    cur.execute(stmt, params)
                else:
                    cur.execute(stmt)
            timings[model.name] = round(time.perf_counter() - start, 3)
        cur.executemany(
            "INSERT INTO ops.model_runs (model, seconds) VALUES (%s, %s)",
            list(timings.items()),
        )
    return {"day_from": day_from.isoformat(), "day_to": day_to.isoformat(), "seconds": timings}
