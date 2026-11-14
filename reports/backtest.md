# Forecast backtest

Pipeline run 2026-11-13. Rolling origins: city weekly, stations every 28 days; horizons 1-7 days. MAE in AQI points, computed only over cells every model predicted.

## MAE by level and model

| level | last_value | prophet | seasonal_naive_7d |
|---|---|---|---|
| city | 11.02 | 11.67 | 13.61 |
| station | 15.15 | 14.3 | 16.38 |

## Cells scored

| level | last_value | prophet | seasonal_naive_7d |
|---|---|---|---|
| city | 159 | 159 | 159 |
| station | 725 | 725 | 725 |

## Origins

| level | first_origin | last_origin | origins |
|---|---|---|---|
| city | 2026-05-15 | 2026-11-06 | 26 |
| station | 2026-05-29 | 2026-10-16 | 6 |

## City MAE by horizon (days ahead)

| horizon_days | last_value | prophet | seasonal_naive_7d |
|---|---|---|---|
| 1 | 6.6 | 9.3 | 13.6 |
| 2 | 11.0 | 13.0 | 16.0 |
| 3 | 12.4 | 13.6 | 14.9 |
| 4 | 8.7 | 9.1 | 10.5 |
| 5 | 11.8 | 12.1 | 13.2 |
| 6 | 14.2 | 14.1 | 14.7 |
| 7 | 12.2 | 10.1 | 12.2 |
