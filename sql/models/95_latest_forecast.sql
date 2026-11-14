-- materialized: view
-- The newest forecast per series (what the Power BI Forecast page shows).
CREATE OR REPLACE VIEW marts.latest_forecast AS
SELECT f.level, f.station_key, f.target_date, f.horizon_days, f.run_date,
       f.aqi_forecast, f.aqi_lower, f.aqi_upper, f.model,
       core.aqi_category(f.aqi_forecast) AS category_forecast
  FROM marts.forecasts f
 WHERE f.run_date = (SELECT max(run_date) FROM marts.forecasts);
