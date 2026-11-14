-- materialized: table
-- Weekly (ISO weeks, Monday start) averages per station, plus city rows (station_key = 'mumbai').
-- measure = 'aqi' or a pollutant; change_pct compares with the previous week of the same series.
DROP TABLE IF EXISTS marts.weekly_trends CASCADE;

CREATE TABLE marts.weekly_trends AS
WITH weekly AS (
    SELECT date_trunc('week', day)::date AS week_start, station_key, 'aqi' AS measure,
           avg(aqi::numeric)::double precision AS value, count(*) AS days
      FROM marts.daily_station_aqi
     WHERE aqi IS NOT NULL
     GROUP BY 1, 2
    UNION ALL
    SELECT date_trunc('week', day)::date, station_key, pollutant, avg(concentration::numeric)::double precision, count(*)
      FROM marts.daily_pollutant
     WHERE valid
     GROUP BY 1, 2, 3
    UNION ALL
    SELECT date_trunc('week', day)::date, 'mumbai', 'aqi', avg(aqi::numeric)::double precision, count(*)
      FROM marts.city_daily_aqi
     GROUP BY 1
    UNION ALL
    SELECT date_trunc('week', p.day)::date, 'mumbai', p.pollutant, avg(p.concentration::numeric)::double precision,
           count(DISTINCT p.day)
      FROM marts.daily_pollutant p
      JOIN marts.dim_station s USING (station_key)
     WHERE p.valid AND s.in_mumbai
     GROUP BY 1, 3
)
SELECT week_start, station_key, measure,
       round(value::numeric, 2)::double precision AS value,
       days,
       round((100.0 * (value / nullif(lag(value) OVER w, 0) - 1))::numeric, 1)::double precision
           AS change_pct
  FROM weekly
WINDOW w AS (PARTITION BY station_key, measure ORDER BY week_start);

ALTER TABLE marts.weekly_trends ADD PRIMARY KEY (week_start, station_key, measure);
