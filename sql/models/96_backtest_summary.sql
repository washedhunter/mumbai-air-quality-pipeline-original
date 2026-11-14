-- materialized: view
-- MAE per level, model and horizon for the latest backtest, over cells every model predicted
-- (same rule as mumbai_aq.forecast.summarise_backtest). The Power BI measure pools these rows
-- weighted by n, which gives the same overall MAE as the README.
CREATE OR REPLACE VIEW marts.backtest_summary AS
WITH latest AS (
    SELECT * FROM marts.forecast_backtest
     WHERE run_date = (SELECT max(run_date) FROM marts.forecast_backtest)
),
complete AS (
    SELECT level, station_key, origin_date, target_date
      FROM latest
     GROUP BY 1, 2, 3, 4
    HAVING count(DISTINCT model) = (SELECT count(DISTINCT model) FROM latest)
)
SELECT l.run_date, l.level, l.model, l.horizon_days,
       round(avg(abs(l.actual - l.predicted))::numeric, 2)::double precision AS mae,
       count(*) AS n
  FROM latest l
  JOIN complete c USING (level, station_key, origin_date, target_date)
 GROUP BY l.run_date, l.level, l.model, l.horizon_days;
