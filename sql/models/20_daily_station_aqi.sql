-- materialized: incremental
-- Daily AQI per station: CPCB minimum-pollutant rule (>= 3 valid sub-indices, one of them PM2.5
-- or PM10); AQI = highest sub-index, whose pollutant is the dominant one.

CREATE TABLE IF NOT EXISTS marts.daily_station_aqi (
    station_key         text    NOT NULL,
    day                 date    NOT NULL,
    aqi                 integer,                      -- NULL when the data are insufficient
    category            text,
    dominant_pollutant  text,
    n_pollutants        integer NOT NULL,             -- pollutants with a valid sub-index
    has_pm              boolean NOT NULL,
    max_sub_index       integer,                      -- highest sub-index even if AQI is NULL
    insufficient_reason text,
    PRIMARY KEY (station_key, day)
);

DELETE FROM marts.daily_station_aqi WHERE day BETWEEN %(day_from)s AND %(day_to)s;

INSERT INTO marts.daily_station_aqi
WITH s AS (
    SELECT station_key, day,
           count(*) FILTER (WHERE valid)                                      AS n_pollutants,
           coalesce(bool_or(valid AND pollutant IN ('pm25', 'pm10')), false)  AS has_pm,
           max(sub_index) FILTER (WHERE valid)                                AS max_si,
           (array_agg(pollutant ORDER BY sub_index DESC, pollutant)
                FILTER (WHERE valid))[1]                                      AS top_pollutant
      FROM marts.daily_pollutant
     WHERE day BETWEEN %(day_from)s AND %(day_to)s
     GROUP BY station_key, day
)
SELECT station_key, day,
       CASE WHEN n_pollutants >= 3 AND has_pm THEN core.round_aqi(max_si) END,
       CASE WHEN n_pollutants >= 3 AND has_pm THEN core.aqi_category(max_si) END,
       CASE WHEN n_pollutants >= 3 AND has_pm THEN top_pollutant END,
       n_pollutants,
       has_pm,
       core.round_aqi(max_si),
       CASE WHEN n_pollutants < 3 THEN 'only ' || n_pollutants || ' pollutants (need 3)'
            WHEN NOT has_pm THEN 'no PM2.5 or PM10' END
  FROM s;
