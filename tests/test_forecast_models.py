from datetime import date

import numpy as np
import pandas as pd
import pytest

from mumbai_aq import forecast, models


def series(n=120, start="2026-01-01", seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="D")
    weekly = 10 * np.sin(2 * np.pi * np.arange(n) / 7)
    return pd.Series(100 + weekly + rng.normal(0, 3, n), index=idx)


def test_baselines():
    s = series(30)
    origin = s.index[-1]
    preds = forecast.baseline_predictions(s, origin, 7)
    assert preds["last_value"] == [s.iloc[-1]] * 7
    # h=1 uses the value 6 days before the origin (same weekday as the target)
    assert preds["seasonal_naive_7d"][0] == s[origin - pd.Timedelta(days=6)]
    assert preds["seasonal_naive_7d"][6] == s[origin]


def test_seasonal_naive_missing_lag_is_none():
    s = series(30).drop(series(30).index[-7])
    preds = forecast.baseline_predictions(s, s.index[-1], 7)
    assert preds["seasonal_naive_7d"][0] is None


def test_fit_predict_shape_and_bounds():
    s = series(120)
    targets = [s.index[-1] + pd.Timedelta(days=h) for h in range(1, 8)]
    out = forecast.fit_predict(s, targets)
    assert len(out) == 7
    assert (out["aqi_lower"] <= out["aqi_forecast"]).all()
    assert (out["aqi_forecast"] <= out["aqi_upper"]).all()
    assert out["aqi_forecast"].between(0, 500).all()
    # A clean weekly pattern should be forecast far better than the series' spread.
    actual = 100 + 10 * np.sin(2 * np.pi * np.arange(120, 127) / 7)
    assert np.abs(out["aqi_forecast"].to_numpy() - actual).mean() < 6


def test_backtest_series_and_summary():
    s = forecast.Series("city", "mumbai", series(120))
    rows = forecast.backtest_series(s, s.data.index[-1].date(), n_origins=3, step_days=7, horizon=7)
    bt = pd.DataFrame(rows)
    assert set(bt["model"]) == {"prophet", "last_value", "seasonal_naive_7d"}
    # origins at 7, 14, 21 days before the run date; the first only has 7 targets in-sample
    assert bt["origin_date"].nunique() == 3
    summary = forecast.summarise_backtest(bt)
    assert set(summary["model"]) == {"prophet", "last_value", "seasonal_naive_7d"}
    assert (summary["n"] == summary["n"].iloc[0]).all()  # same cells for every model
    mae = summary.set_index("model")["mae"]
    assert mae["prophet"] < mae["last_value"]


def test_summary_of_nothing():
    assert forecast.summarise_backtest(pd.DataFrame()).empty


def test_every_model_has_a_header_and_splits():
    found = models.discover()
    assert [m.name for m in found] == sorted(m.name for m in found)
    kinds = {m.name: m.materialized for m in found}
    assert kinds["10_daily_pollutant"] == "incremental"
    assert kinds["20_daily_station_aqi"] == "incremental"
    assert kinds["95_latest_forecast"] == "view"
    for m in found:
        stmts = m.statements()
        assert stmts, m.name
        if m.materialized == "incremental":
            # Incremental models must delete their day range before inserting it.
            assert any(s.upper().startswith("DELETE") and "%(day_from)s" in s for s in stmts), m.name
        else:
            assert all("%(" not in s for s in stmts), m.name


def test_model_without_header_is_rejected(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "01_x.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="materialized"):
        models.discover(tmp_path)


def test_settings_source_override():
    from mumbai_aq.config import Settings

    s = Settings()
    assert s.source_for(date(2020, 1, 1), date(2026, 1, 1)) in {"archive", s.extract_source}
