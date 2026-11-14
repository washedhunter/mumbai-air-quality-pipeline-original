-- materialized: table
-- Seasonal patterns (IMD seasons) per neighbourhood and for the city (level = 'city', area =
-- 'Mumbai'): mean and 90th-percentile concentration and mean sub-index per pollutant, plus AQI
-- rows (pollutant = 'aqi') with the share of days rated Poor or worse (AQI > 200).
DROP TABLE IF EXISTS marts.seasonal_patterns CASCADE;

CREATE TABLE marts.seasonal_patterns AS
WITH p AS (
    SELECT d.day, s.neighbourhood, s.zone, s.in_mumbai, d.pollutant, d.concentration, d.sub_index
      FROM marts.daily_pollutant d
      JOIN marts.dim_station s USING (station_key)
     WHERE d.valid
),
a AS (
    SELECT d.day, s.neighbourhood, s.zone, s.in_mumbai, d.aqi
      FROM marts.daily_station_aqi d
      JOIN marts.dim_station s USING (station_key)
     WHERE d.aqi IS NOT NULL
),
tagged AS (
    SELECT 'neighbourhood' AS level, neighbourhood AS area, zone, pollutant, day,
           concentration AS value, sub_index
      FROM p
    UNION ALL
    SELECT 'city', 'Mumbai', 'Mumbai', pollutant, day, concentration, sub_index
      FROM p WHERE in_mumbai
    UNION ALL
    SELECT 'neighbourhood', neighbourhood, zone, 'aqi', day, aqi::double precision, aqi
      FROM a
    UNION ALL
    SELECT 'city', 'Mumbai', 'Mumbai', 'aqi', day, aqi::double precision, aqi
      FROM a WHERE in_mumbai
)
SELECT t.level, t.area, t.zone, s.season, s.season_order,
       extract(year FROM t.day)::int                                      AS year,
       s.season || ' ' || extract(year FROM t.day)::int                   AS season_label,
       t.pollutant,
       round(avg(t.value::numeric), 2)::double precision                  AS mean_value,
       round((percentile_cont(0.9) WITHIN GROUP (ORDER BY t.value))::numeric, 2)::double precision
                                                                          AS p90_value,
       round(avg(t.sub_index::numeric), 1)::double precision              AS mean_sub_index,
       count(DISTINCT t.day)                                              AS days,
       CASE WHEN t.pollutant = 'aqi' THEN
           round((100.0 * avg((t.value > 200)::int))::numeric, 1)::double precision
       END                                                                AS pct_poor_or_worse
  FROM tagged t
  JOIN core.seasons s ON s.month = extract(month FROM t.day)::int
 GROUP BY t.level, t.area, t.zone, s.season, s.season_order, extract(year FROM t.day), t.pollutant;

ALTER TABLE marts.seasonal_patterns ADD PRIMARY KEY (level, area, season, year, pollutant);
