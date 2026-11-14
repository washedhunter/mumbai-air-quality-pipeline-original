-- materialized: table
-- Mumbai city AQI per day = mean AQI of the stations inside Mumbai (island city + suburbs) that
-- have a valid AQI that day, only on days with at least 3 such stations (so one or two stations
-- never stand in for the city). Our definition; the median and station count sit alongside.
DROP TABLE IF EXISTS marts.city_daily_aqi CASCADE;

CREATE TABLE marts.city_daily_aqi AS
SELECT a.day,
       round(avg(a.aqi::numeric), 1)::double precision       AS aqi,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY a.aqi)     AS aqi_median,
       min(a.aqi)                                              AS aqi_min,
       max(a.aqi)                                              AS aqi_max,
       count(*)                                                AS n_stations,
       core.aqi_category(avg(a.aqi))                           AS category,
       mode() WITHIN GROUP (ORDER BY a.dominant_pollutant)     AS dominant_pollutant
  FROM marts.daily_station_aqi a
  JOIN marts.dim_station s USING (station_key)
 WHERE a.aqi IS NOT NULL AND s.in_mumbai
 GROUP BY a.day
HAVING count(*) >= 3;

ALTER TABLE marts.city_daily_aqi ADD PRIMARY KEY (day);
