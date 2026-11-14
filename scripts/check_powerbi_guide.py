"""Check that every table[column] and measure named in the Power BI guide exists.

    uv run python scripts/check_powerbi_guide.py          (uses WAREHOUSE_DSN)

Exit code 0 = everything the guide and measures.dax reference is in the warehouse / defined.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import psycopg

from mumbai_aq.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "powerbi" / "BUILD_GUIDE.md"
DAX = ROOT / "powerbi" / "measures.dax"
REF = re.compile(r"\b([a-z_]+)\[([a-z0-9_]+)\]")
MEASURE_DEF = re.compile(r"^([A-Z%][^=\n]*?) =\s*$", re.MULTILINE)


def defined_measures(dax: str) -> set[str]:
    return {m.strip() for m in MEASURE_DEF.findall(dax)}


def referenced(text: str) -> set[tuple[str, str]]:
    return set(REF.findall(text))


def check(dsn: str) -> list[str]:
    guide = GUIDE.read_text(encoding="utf-8")
    dax = DAX.read_text(encoding="utf-8")
    measures = defined_measures(dax)
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """SELECT table_name, column_name FROM information_schema.columns
                WHERE table_schema = 'marts'"""
        ).fetchall()
    columns: dict[str, set[str]] = {}
    for t, c in rows:
        columns.setdefault(t, set()).add(c)

    problems = []
    for table, column in sorted(referenced(guide) | referenced(dax)):
        if table not in columns:
            problems.append(f"table marts.{table} does not exist (referenced as {table}[{column}])")
        elif column not in columns[table]:
            problems.append(f"column marts.{table}.{column} does not exist")
    for name in re.findall(r"`marts\.([a-z_]+)`", guide):
        if name not in columns:
            problems.append(f"Navigator table marts.{name} does not exist")
    # Measures used in the guide (backticked names that match a DAX definition style)
    for name in re.findall(r"`([A-Z%][A-Za-z0-9 %().\-]+)`", guide):
        if name in {"AQI", "Load", "OK", "Column1"} or name.startswith(("Win", "File", "Home")):
            continue
        if name not in measures:
            problems.append(
                f"measure '{name}' is used in the guide but not defined in measures.dax"
            )
    # Measures referenced inside other measures
    for name in re.findall(r"\[([A-Z%][^\]]+)\]", dax):
        if name not in measures:
            problems.append(f"measure [{name}] is referenced in measures.dax but not defined")
    return problems


def main() -> int:
    problems = check(get_settings().warehouse_dsn)
    for p in problems:
        print("PROBLEM:", p)
    if not problems:
        print("OK: every table, column and measure in the Power BI guide exists")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
