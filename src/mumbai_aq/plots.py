"""Charts for the README, drawn from the warehouse (real pipeline output, nothing hand-made)."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from . import db
from .config import Settings

BANDS = [
    (0, 50, "#00B050", "Good"),
    (50, 100, "#92D050", "Satisfactory"),
    (100, 200, "#FFFF00", "Moderate"),
    (200, 300, "#FF9900", "Poor"),
    (300, 400, "#FF0000", "Very Poor"),
    (400, 500, "#C00000", "Severe"),
]
MODEL_LABELS = {
    "prophet": "Prophet",
    "last_value": "Last value",
    "seasonal_naive_7d": "Seasonal naive (7d)",
}


def _q(cur, sql: str) -> pd.DataFrame:
    cur.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])


def _bands(ax, top: float) -> None:
    for lo, hi, colour, _ in BANDS:
        if lo < top:
            ax.axhspan(lo, min(hi, top), color=colour, alpha=0.12, lw=0)


def write_plots(settings: Settings, img_dir: Path) -> list[Path]:
    img_dir.mkdir(parents=True, exist_ok=True)
    with db.connect(settings) as conn, conn.cursor() as cur:
        city = _q(cur, "SELECT day, aqi, n_stations FROM marts.city_daily_aqi ORDER BY day")
        fc = _q(
            cur,
            """SELECT target_date, aqi_forecast, aqi_lower, aqi_upper, run_date
                          FROM marts.latest_forecast WHERE level = 'city' ORDER BY target_date""",
        )
        bt = _q(
            cur,
            "SELECT level, model, horizon_days, mae FROM marts.backtest_summary WHERE level = 'city'",
        )
        seas = _q(
            cur,
            """SELECT area, season, season_order, avg(mean_value) AS aqi
                            FROM marts.seasonal_patterns
                           WHERE level = 'neighbourhood' AND pollutant = 'aqi' AND zone <> 'Unmapped'
                           GROUP BY area, season, season_order""",
        )
    paths: list[Path] = []

    if not city.empty:
        city["day"] = pd.to_datetime(city["day"])
        s = city.set_index("day")["aqi"].astype(float)
        fig, ax = plt.subplots(figsize=(11, 4.2))
        peak = max(float(s.max()), float(fc["aqi_upper"].max()) if not fc.empty else 0.0)
        top = min(500.0, max(250.0, peak * 1.1))
        _bands(ax, top)
        ax.plot(s.index, s.values, color="#1F4E79", lw=0.8, alpha=0.55, label="Daily city AQI")
        ax.plot(
            s.index, s.rolling(7, min_periods=4).mean(), color="#1F4E79", lw=1.8, label="7-day mean"
        )
        if not fc.empty:
            fc["target_date"] = pd.to_datetime(fc["target_date"])
            ax.fill_between(
                fc["target_date"],
                fc["aqi_lower"],
                fc["aqi_upper"],
                color="#F18F01",
                alpha=0.3,
                lw=0,
                label="80% interval",
            )
            ax.plot(
                fc["target_date"],
                fc["aqi_forecast"],
                color="#F18F01",
                lw=2,
                label=f"Forecast (from {fc['run_date'].iloc[0]})",
            )
        ax.set_ylim(0, top)
        ax.set_ylabel("CPCB AQI")
        ax.set_title("Mumbai daily AQI (mean of city stations) with the 7-day Prophet forecast")
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
        ax.legend(loc="upper left", fontsize=8, ncols=2, frameon=False)
        ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        p = img_dir / "city_aqi.png"
        fig.savefig(p, dpi=130)
        plt.close(fig)
        paths.append(p)

    if not bt.empty:
        pivot = bt.pivot_table(index="horizon_days", columns="model", values="mae")
        fig, ax = plt.subplots(figsize=(7, 3.6))
        for model in ["prophet", "seasonal_naive_7d", "last_value"]:
            if model in pivot:
                ax.plot(pivot.index, pivot[model], marker="o", label=MODEL_LABELS[model])
        ax.set_xlabel("Days ahead")
        ax.set_ylabel("MAE (AQI points)")
        ax.set_title("City AQI backtest: error by forecast horizon")
        ax.grid(alpha=0.3)
        ax.legend(frameon=False, fontsize=8)
        fig.tight_layout()
        p = img_dir / "backtest_mae.png"
        fig.savefig(p, dpi=130)
        plt.close(fig)
        paths.append(p)

    if not seas.empty:
        order = seas.drop_duplicates("season").sort_values("season_order")["season"].tolist()
        grid = seas.pivot_table(index="area", columns="season", values="aqi")[order]
        grid = grid.loc[grid.mean(axis=1).sort_values(ascending=False).index]
        fig, ax = plt.subplots(figsize=(7, 0.32 * len(grid) + 1.4))
        im = ax.imshow(
            grid.to_numpy(dtype=float), cmap="RdYlGn_r", vmin=30, vmax=250, aspect="auto"
        )
        ax.set_xticks(range(len(order)), order)
        ax.set_yticks(range(len(grid)), grid.index, fontsize=8)
        for i in range(grid.shape[0]):
            for j in range(grid.shape[1]):
                v = grid.iat[i, j]
                if pd.notna(v):
                    ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=7)
        ax.set_title("Mean AQI by neighbourhood and season")
        fig.colorbar(im, ax=ax, fraction=0.04, label="AQI")
        fig.tight_layout()
        p = img_dir / "neighbourhood_season.png"
        fig.savefig(p, dpi=130)
        plt.close(fig)
        paths.append(p)
    return paths
