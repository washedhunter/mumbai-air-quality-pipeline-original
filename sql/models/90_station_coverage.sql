-- materialized: table
-- What data each station actually has (documents real coverage; also used by the DQ checks).
DROP TABLE IF EXISTS marts.station_coverage CASCADE;

CREATE TABLE marts.station_coverage AS
WITH span AS (
    SELECT min(day_ist) AS d0, max(day_ist) AS d1 FROM core.hourly
),
h AS (
    SELECT station_key, min(day_ist) AS first_day, max(day_ist) AS last_day,
           count(DISTINCT day_ist) AS days_with_data,
           string_agg(DISTINCT parameter, ',' ORDER BY parameter) AS pollutants
      FROM core.hourly
     GROUP BY station_key
),
a AS (
    SELECT station_key, count(*) FILTER (WHERE aqi IS NOT NULL) AS days_with_aqi
      FROM marts.daily_station_aqi
     GROUP BY station_key
)
SELECT h.station_key, s.station_name, s.neighbourhood, s.zone, h.first_day, h.last_day,
       h.days_with_data,
       coalesce(a.days_with_aqi, 0)          AS days_with_aqi,
       (span.d1 - span.d0 + 1)               AS days_in_window,
       round((100.0 * coalesce(a.days_with_aqi, 0) / (span.d1 - span.d0 + 1))::numeric, 1)::double precision
                                             AS pct_days_with_aqi,
       h.pollutants
  FROM h
 CROSS JOIN span
  LEFT JOIN a USING (station_key)
  LEFT JOIN marts.dim_station s USING (station_key);

ALTER TABLE marts.station_coverage ADD PRIMARY KEY (station_key);
