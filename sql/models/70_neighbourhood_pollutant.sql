-- materialized: table
-- Neighbourhood x pollutant comparison per month: mean concentration and sub-index, rank among
-- neighbourhoods (1 = most polluted) and ratio to the mean across neighbourhoods that month.
DROP TABLE IF EXISTS marts.neighbourhood_pollutant CASCADE;

CREATE TABLE marts.neighbourhood_pollutant AS
WITH m AS (
    SELECT date_trunc('month', d.day)::date AS month_start, s.neighbourhood, s.zone, s.in_mumbai,
           d.pollutant,
           avg(d.concentration::numeric)  AS mean_conc,
           avg(d.sub_index::numeric)      AS mean_si,
           count(DISTINCT d.station_key)   AS n_stations,
           count(DISTINCT d.day)           AS days
      FROM marts.daily_pollutant d
      JOIN marts.dim_station s USING (station_key)
     WHERE d.valid
     GROUP BY 1, 2, 3, 4, 5
)
SELECT month_start, neighbourhood, zone, in_mumbai, pollutant,
       round(mean_conc, 2)::double precision  AS mean_concentration,
       round(mean_si, 1)::double precision    AS mean_sub_index,
       n_stations, days,
       rank() OVER (PARTITION BY month_start, pollutant ORDER BY mean_conc DESC) AS rank_in_month,
       round(mean_conc / nullif(avg(mean_conc) OVER (PARTITION BY month_start, pollutant), 0), 2)::double precision
           AS ratio_to_average
  FROM m;

ALTER TABLE marts.neighbourhood_pollutant ADD PRIMARY KEY (month_start, neighbourhood, pollutant);
