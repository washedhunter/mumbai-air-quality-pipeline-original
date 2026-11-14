-- materialized: table
-- How often each pollutant is the dominant (AQI-setting) pollutant, per station and month.
DROP TABLE IF EXISTS marts.dominant_pollutant CASCADE;

CREATE TABLE marts.dominant_pollutant AS
WITH c AS (
    SELECT date_trunc('month', day)::date AS month_start,
           station_key,
           dominant_pollutant AS pollutant,
           count(*) AS days_dominant
      FROM marts.daily_station_aqi
     WHERE aqi IS NOT NULL
     GROUP BY 1, 2, 3
)
SELECT month_start, station_key, pollutant, days_dominant,
       round((100.0 * days_dominant
              / sum(days_dominant) OVER (PARTITION BY month_start, station_key))::numeric, 1)::double precision
           AS pct_of_days
  FROM c;

ALTER TABLE marts.dominant_pollutant ADD PRIMARY KEY (month_start, station_key, pollutant);
