"""Markdown/JSON reports generated from the warehouse. README numbers are copied from these."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from . import db
from .config import Settings
from .forecast import summarise_backtest

COUNT_TABLES = (
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
    "marts.forecast_backtest",
)


def _frame(cur, sql: str, params: tuple = ()) -> pd.DataFrame:
    cur.execute(sql, params)
    cols = [c.name for c in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


def _md(df: pd.DataFrame) -> str:
    if df.empty:
        return "_(no rows)_\n"
    header = "| " + " | ".join(map(str, df.columns)) + " |"
    sep = "|" + "|".join("---" for _ in df.columns) + "|"
    rows = [
        "| " + " | ".join("" if pd.isna(v) else str(v) for v in r) + " |"
        for r in df.itertuples(index=False)
    ]
    return "\n".join([header, sep, *rows]) + "\n"


def write_reports(settings: Settings, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with db.connect(settings) as conn, conn.cursor() as cur:
        counts = {}
        for t in COUNT_TABLES:
            cur.execute(f"SELECT count(*) FROM {t}")
            counts[t] = cur.fetchone()[0]
        span = _frame(
            cur,
            "SELECT min(day_ist) AS first_day, max(day_ist) AS last_day, count(DISTINCT day_ist) AS days FROM core.hourly",
        )
        city = _frame(
            cur,
            "SELECT count(*) AS days, min(day) AS first_day, max(day) AS last_day FROM marts.city_daily_aqi WHERE n_stations >= 3",
        )
        run = _frame(cur, "SELECT max(run_date) AS run_date FROM marts.forecast_backtest")
        run_date = run["run_date"].iloc[0]
        bt = (
            _frame(cur, "SELECT * FROM marts.forecast_backtest WHERE run_date = %s", (run_date,))
            if run_date
            else pd.DataFrame()
        )
        cov = _frame(
            cur,
            """SELECT station_key, station_name, neighbourhood, zone, first_day, last_day,
                      days_with_data, days_with_aqi, pct_days_with_aqi, pollutants
                 FROM marts.station_coverage ORDER BY zone, neighbourhood, station_key""",
        )
        fc = _frame(
            cur,
            """SELECT target_date, round(aqi_forecast::numeric, 0) AS aqi_forecast,
                      round(aqi_lower::numeric, 0) AS lower_80, round(aqi_upper::numeric, 0) AS upper_80,
                      core.aqi_category(aqi_forecast) AS category
                 FROM marts.latest_forecast WHERE level = 'city' ORDER BY target_date""",
        )

    paths = []
    # Backtest
    lines = ["# Forecast backtest", ""]
    if bt.empty:
        lines.append("No backtest yet (run a non-backfill day first).")
    else:
        summary = summarise_backtest(bt)
        origins = bt.groupby("level")["origin_date"].agg(["min", "max", "nunique"]).reset_index()
        lines += [
            f"Pipeline run {run_date}. Rolling origins: city weekly, stations every 28 days; "
            "horizons 1-7 days. MAE in AQI points, computed only over cells every model predicted.",
            "",
            "## MAE by level and model",
            "",
            _md(summary.pivot(index="level", columns="model", values="mae").reset_index()),
            "## Cells scored",
            "",
            _md(summary.pivot(index="level", columns="model", values="n").reset_index()),
            "## Origins",
            "",
            _md(
                origins.rename(
                    columns={"min": "first_origin", "max": "last_origin", "nunique": "origins"}
                )
            ),
        ]
        keys = ["level", "station_key", "origin_date", "target_date"]
        complete = bt.groupby(keys)["model"].transform("nunique") == bt["model"].nunique()
        city = bt[complete & (bt["level"] == "city")].assign(
            abs_err=lambda d: (d["actual"] - d["predicted"]).abs()
        )
        if not city.empty:
            by_h = (
                city.groupby(["horizon_days", "model"])["abs_err"]
                .mean()
                .round(1)
                .unstack()
                .reset_index()
            )
            lines += ["## City MAE by horizon (days ahead)", "", _md(by_h)]
    p = out_dir / "backtest.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    paths.append(p)

    # Coverage
    p = out_dir / "coverage.md"
    p.write_text(
        "# Station coverage\n\n"
        f"Hourly data from {span['first_day'].iloc[0]} to {span['last_day'].iloc[0]} "
        f"({span['days'].iloc[0]} days). City AQI (>= 3 Mumbai stations) on {city['days'].iloc[0]} days.\n\n"
        + _md(cov),
        encoding="utf-8",
    )
    paths.append(p)

    p = out_dir / "summary.json"
    p.write_text(
        json.dumps(
            {
                "row_counts": counts,
                "hourly_span": span.astype(str).to_dict(orient="records")[0],
                "city_aqi_days": int(city["days"].iloc[0]),
                "stations_with_data": len(cov),
                "backtest_run_date": str(run_date) if run_date else None,
                "backtest_mae": summarise_backtest(bt).to_dict(orient="records")
                if not bt.empty
                else [],
                "latest_city_forecast": fc.astype(str).to_dict(orient="records"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    paths.append(p)
    return paths
