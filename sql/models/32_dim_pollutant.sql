-- materialized: table
-- Display names, units and sort order for pollutants (and 'aqi'), for report labels.
DROP TABLE IF EXISTS marts.dim_pollutant CASCADE;

CREATE TABLE marts.dim_pollutant (
    pollutant     text PRIMARY KEY,
    display_name  text    NOT NULL,
    unit          text    NOT NULL,
    averaging     text    NOT NULL,
    sort_order    integer NOT NULL
);

INSERT INTO marts.dim_pollutant VALUES
    ('aqi',  'AQI',   'index', 'daily (CPCB)',  0),
    ('pm25', 'PM2.5', 'µg/m³', '24-hour mean',  1),
    ('pm10', 'PM10',  'µg/m³', '24-hour mean',  2),
    ('no2',  'NO₂',   'µg/m³', '24-hour mean',  3),
    ('so2',  'SO₂',   'µg/m³', '24-hour mean',  4),
    ('o3',   'O₃',    'µg/m³', 'max 8-hour mean', 5),
    ('co',   'CO',    'mg/m³', 'max 8-hour mean', 6),
    ('nh3',  'NH₃',   'µg/m³', '24-hour mean',  7);
