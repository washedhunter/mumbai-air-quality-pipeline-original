"""7-day AQI forecasts with Prophet, and a rolling-origin backtest against naive baselines.

- City series: marts.city_daily_aqi (days with >= 3 Mumbai stations).
- Station series: stations with enough history that are still reporting.
- Prophet models log(AQI) (AQI is right-skewed and must stay positive), with weekly seasonality
  and yearly seasonality once there is a year of data. 80% intervals. Output clipped to 0-500.
- Baselines: last observed value, and seasonal naive (same weekday one week earlier).
"""

from __future__ import annotations

import logging
import zlib
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
import psycopg

from .config import Settings

MODEL_NAME = "prophet-log-v1"
BASELINES = ("last_value", "seasonal_naive_7d")

for _name in ("cmdstanpy", "prophet"):
    logging.getLogger(_name).setLevel(logging.WARNING)


@dataclass(frozen=True)
class Series:
    level: str
    key: str
    data: pd.Series  # index: day (Timestamp), values: AQI


def fit_predict(
    history: pd.Series, horizon_dates: list[pd.Timestamp], seed_key: str = ""
) -> pd.DataFrame:
    """Fit Prophet on `history` (daily AQI) and predict `horizon_dates`.

    Prophet draws its uncertainty intervals by random sampling from numpy's global generator, so
    it is seeded from the series and dates: re-running a day reproduces the same intervals."""
    from prophet import Prophet  # imported lazily: slow import, not needed by the other tasks

    history = history.dropna()
    seed_text = f"{seed_key}|{history.index.max().date()}|{len(history)}"
    np.random.seed(zlib.crc32(seed_text.encode()))
    span_days = (history.index.max() - history.index.min()).days
    model = Prophet(
        yearly_seasonality=span_days >= 365,
        weekly_seasonality=True,
        daily_seasonality=False,
        interval_width=0.8,
        uncertainty_samples=300,
    )
    frame = pd.DataFrame({"ds": history.index, "y": np.log(history.clip(lower=1).to_numpy())})
    model.fit(frame)
    pred = model.predict(pd.DataFrame({"ds": horizon_dates}))
    out = pd.DataFrame(
        {
            "target_date": pd.to_datetime(pred["ds"]).dt.date,
            "aqi_forecast": np.exp(pred["yhat"]).clip(0, 500),
            "aqi_lower": np.exp(pred["yhat_lower"]).clip(0, 500),
            "aqi_upper": np.exp(pred["yhat_upper"]).clip(0, 500),
        }
    )
    return out


def baseline_predictions(history: pd.Series, origin: pd.Timestamp, horizon: int) -> dict[str, list]:
    """Naive forecasts for origin+1 .. origin+horizon (None where the baseline has no value)."""
    hist = history.dropna()
    hist = hist[hist.index <= origin]
    last = float(hist.iloc[-1]) if len(hist) else None
    seasonal = []
    for h in range(1, horizon + 1):
        # Same weekday, one week before the target; for h > 7 roll forward whole weeks.
        lag_day = origin + pd.Timedelta(days=h) - pd.Timedelta(days=7 * ((h - 1) // 7 + 1))
        seasonal.append(float(hist[lag_day]) if lag_day in hist.index else None)
    return {"last_value": [last] * horizon, "seasonal_naive_7d": seasonal}


def load_series(conn: psycopg.Connection, settings: Settings, run_date: date) -> list[Series]:
    out: list[Series] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT day, aqi FROM marts.city_daily_aqi WHERE day <= %s ORDER BY day",
            (run_date,),
        )
        city = cur.fetchall()
        cur.execute(
            """SELECT station_key, day, aqi FROM marts.daily_station_aqi
                WHERE aqi IS NOT NULL AND day <= %s ORDER BY station_key, day""",
            (run_date,),
        )
        stations = cur.fetchall()
    if len(city) >= settings.forecast_min_days_city:
        s = pd.Series([float(a) for _, a in city], index=pd.to_datetime([d for d, _ in city]))
        out.append(Series("city", "mumbai", s))
    frame = pd.DataFrame(stations, columns=["station_key", "day", "aqi"])
    for key, g in frame.groupby("station_key"):
        s = pd.Series(g["aqi"].astype(float).to_numpy(), index=pd.to_datetime(g["day"]))
        recent = s.index.max() >= pd.Timestamp(run_date) - pd.Timedelta(days=7)
        if len(s) >= settings.forecast_min_days_station and recent:
            out.append(Series("station", str(key), s))
    return out


def backtest_series(
    series: Series, run_date: date, n_origins: int, step_days: int, horizon: int
) -> list[dict]:
    rows: list[dict] = []
    data = series.data
    last_actual = data.index.max()
    for k in range(1, n_origins + 1):
        origin = pd.Timestamp(run_date) - pd.Timedelta(days=step_days * k)
        if origin + pd.Timedelta(days=1) > last_actual:
            continue
        hist = data[data.index <= origin]
        if len(hist) < 30:
            continue
        targets = [origin + pd.Timedelta(days=h) for h in range(1, horizon + 1)]
        prophet = fit_predict(hist, targets, f"{series.level}:{series.key}")[
            "aqi_forecast"
        ].tolist()
        preds = {"prophet": prophet, **baseline_predictions(hist, origin, horizon)}
        for h, target in enumerate(targets, start=1):
            if target not in data.index:
                continue
            actual = float(data[target])
            for model, values in preds.items():
                if values[h - 1] is None:
                    continue
                rows.append(
                    {
                        "run_date": run_date,
                        "level": series.level,
                        "station_key": series.key,
                        "origin_date": origin.date(),
                        "target_date": target.date(),
                        "horizon_days": h,
                        "model": model,
                        "actual": actual,
                        "predicted": float(values[h - 1]),
                    }
                )
    return rows


def run_forecast(conn: psycopg.Connection, settings: Settings, run_date: date) -> dict:
    horizon = settings.forecast_horizon_days
    series = load_series(conn, settings, run_date)
    forecasts: list[tuple] = []
    backtest: list[dict] = []
    targets = [pd.Timestamp(run_date) + pd.Timedelta(days=h) for h in range(1, horizon + 1)]
    for s in series:
        pred = fit_predict(s.data, targets, f"{s.level}:{s.key}")
        for h, row in enumerate(pred.itertuples(index=False), start=1):
            forecasts.append(
                (
                    run_date,
                    s.level,
                    s.key,
                    row.target_date,
                    h,
                    float(row.aqi_forecast),
                    float(row.aqi_lower),
                    float(row.aqi_upper),
                    MODEL_NAME,
                )
            )
        if s.level == "city":
            backtest += backtest_series(s, run_date, settings.backtest_weeks, 7, horizon)
        elif settings.backtest_station_origins > 0:
            backtest += backtest_series(s, run_date, settings.backtest_station_origins, 28, horizon)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM marts.forecasts WHERE run_date = %s", (run_date,))
        cur.execute("DELETE FROM marts.forecast_backtest WHERE run_date = %s", (run_date,))
        cur.executemany(
            """INSERT INTO marts.forecasts (run_date, level, station_key, target_date, horizon_days,
                   aqi_forecast, aqi_lower, aqi_upper, model)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            forecasts,
        )
        cur.executemany(
            """INSERT INTO marts.forecast_backtest (run_date, level, station_key, origin_date,
                   target_date, horizon_days, model, actual, predicted)
               VALUES (%(run_date)s, %(level)s, %(station_key)s, %(origin_date)s, %(target_date)s,
                   %(horizon_days)s, %(model)s, %(actual)s, %(predicted)s)""",
            backtest,
        )
    summary = summarise_backtest(pd.DataFrame(backtest))
    return {
        "run_date": run_date.isoformat(),
        "series": len(series),
        "forecast_rows": len(forecasts),
        "backtest_rows": len(backtest),
        "mae": summary.to_dict(orient="records") if len(summary) else [],
    }


def summarise_backtest(bt: pd.DataFrame) -> pd.DataFrame:
    """MAE per level and model, only over (series, origin, target) cells every model predicted."""
    if bt.empty:
        return pd.DataFrame(columns=["level", "model", "mae", "n"])
    keys = ["level", "station_key", "origin_date", "target_date"]
    n_models = bt["model"].nunique()
    complete = bt.groupby(keys)["model"].transform("nunique") == n_models
    bt = bt[complete].assign(abs_err=lambda d: (d["actual"] - d["predicted"]).abs())
    return (
        bt.groupby(["level", "model"])
        .agg(mae=("abs_err", "mean"), n=("abs_err", "size"))
        .reset_index()
        .round({"mae": 2})
    )
