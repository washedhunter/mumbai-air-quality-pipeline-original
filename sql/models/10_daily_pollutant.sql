-- materialized: incremental
-- Daily concentration and CPCB sub-index per station and pollutant.
-- 24-hour pollutants: mean of the day's hourly values. CO / O3: max of 8-hour rolling means
-- (windows ending at the day's hours, each needing >= 6 of 8 hours). A sub-index needs >= 16 hours.
-- Averages use exact numeric arithmetic and 4 decimals so results never depend on row order.

CREATE TABLE IF NOT EXISTS marts.daily_pollutant (
    station_key     text             NOT NULL,
    day             date             NOT NULL,
    pollutant       text             NOT NULL,
    concentration   double precision NOT NULL,       -- µg/m³ (CO: mg/m³)
    averaging       text             NOT NULL,       -- 24h_mean | max_8h_mean
    hours_with_data integer          NOT NULL,
    valid           boolean          NOT NULL,       -- enough data for a sub-index
    sub_index       double precision,                -- NULL when not valid
    PRIMARY KEY (station_key, day, pollutant)
);

DELETE FROM marts.daily_pollutant WHERE day BETWEEN %(day_from)s AND %(day_to)s;

INSERT INTO marts.daily_pollutant
WITH h AS (
    SELECT station_key, parameter, hour_ist, value
      FROM core.hourly
     WHERE hour_ist >= %(day_from)s::date - interval '7 hours'
       AND hour_ist <  %(day_to)s::date + interval '1 day'
),
hours_per_day AS (
    SELECT station_key, parameter, hour_ist::date AS day, count(*) AS hours,
           round(avg(value::numeric), 4)::double precision AS mean_24h
      FROM h
     WHERE hour_ist >= %(day_from)s::date
     GROUP BY 1, 2, 3
),
rolling AS (
    SELECT station_key, parameter, hour_ist,
           round(avg(value::numeric) OVER w, 4)::double precision AS mean_8h,
           count(*)  OVER w    AS n_8h
      FROM h
     WHERE parameter IN ('co', 'o3')
    WINDOW w AS (PARTITION BY station_key, parameter ORDER BY hour_ist
                 RANGE BETWEEN interval '7 hours' PRECEDING AND CURRENT ROW)
),
max_8h AS (
    SELECT station_key, parameter, hour_ist::date AS day, max(mean_8h) AS conc
      FROM rolling
     WHERE n_8h >= 6 AND hour_ist >= %(day_from)s::date
     GROUP BY 1, 2, 3
),
daily AS (
    SELECT d.station_key, d.day, d.parameter AS pollutant,
           CASE WHEN d.parameter IN ('co', 'o3') THEN m.conc ELSE d.mean_24h END AS concentration,
           CASE WHEN d.parameter IN ('co', 'o3') THEN 'max_8h_mean' ELSE '24h_mean' END AS averaging,
           d.hours
      FROM hours_per_day d
      LEFT JOIN max_8h m USING (station_key, parameter, day)
)
SELECT station_key, day, pollutant, concentration, averaging, hours,
       hours >= 16 AS valid,
       CASE WHEN hours >= 16 THEN core.aqi_subindex(pollutant, concentration) END AS sub_index
  FROM daily
 WHERE concentration IS NOT NULL;
