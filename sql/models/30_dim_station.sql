-- materialized: table
-- One row per station (several OpenAQ location IDs can map to one station), plus any location
-- the pipeline saw that is not in config/stations.csv yet.
DROP TABLE IF EXISTS marts.dim_station CASCADE;

CREATE TABLE marts.dim_station AS
WITH mapped AS (
    SELECT DISTINCT ON (station_key)
           station_key, station_name, operator, neighbourhood, zone, in_mumbai, lat, lon
      FROM core.stations
     ORDER BY station_key, location_id DESC
),
ids AS (
    SELECT station_key,
           string_agg(location_id::text, ', ' ORDER BY location_id) AS openaq_location_ids
      FROM core.stations
     GROUP BY station_key
),
unmapped AS (
    SELECT DISTINCT h.station_key
      FROM core.hourly h
     WHERE NOT EXISTS (SELECT 1 FROM core.stations s WHERE s.station_key = h.station_key)
)
SELECT m.*, i.openaq_location_ids
  FROM mapped m JOIN ids i USING (station_key)
UNION ALL
SELECT u.station_key, u.station_key, NULL, 'Unmapped', 'Unmapped', false, NULL, NULL,
       replace(u.station_key, 'loc_', '')
  FROM unmapped u;

ALTER TABLE marts.dim_station ADD PRIMARY KEY (station_key);
