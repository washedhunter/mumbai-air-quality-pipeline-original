-- materialized: table
-- CPCB AQI categories with their official colours (for legends and conditional formatting).
DROP TABLE IF EXISTS marts.dim_aqi_category CASCADE;

CREATE TABLE marts.dim_aqi_category AS
SELECT b.category,
       b.band      AS sort_order,
       b.i_lo      AS aqi_min,
       b.i_hi      AS aqi_max,
       c.colour,
       c.health_impact
  FROM core.aqi_breakpoints b
  JOIN (VALUES
        (1, '#00B050', 'Minimal impact'),
        (2, '#92D050', 'Minor breathing discomfort to sensitive people'),
        (3, '#FFFF00', 'Breathing discomfort to people with lung or heart disease, children and older adults'),
        (4, '#FF9900', 'Breathing discomfort to most people on prolonged exposure'),
        (5, '#FF0000', 'Respiratory illness on prolonged exposure'),
        (6, '#C00000', 'Affects healthy people and seriously impacts those with existing diseases')
       ) AS c(band, colour, health_impact) ON c.band = b.band
 WHERE b.pollutant = 'pm25';

ALTER TABLE marts.dim_aqi_category ADD PRIMARY KEY (category);
