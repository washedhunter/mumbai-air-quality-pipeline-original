-- materialized: table
-- Calendar for Power BI: every day from the first reading to 14 days after the last one
-- (so the forecast horizon is covered).
DROP TABLE IF EXISTS marts.dim_date CASCADE;

CREATE TABLE marts.dim_date AS
WITH bounds AS (
    SELECT coalesce(min(day_ist), current_date) AS d0,
           coalesce(max(day_ist), current_date) + 14 AS d1,
           coalesce(max(day_ist), current_date) AS latest
      FROM core.hourly
)
SELECT d::date                                       AS day,
       extract(year FROM d)::int                     AS year,
       extract(month FROM d)::int                    AS month,
       to_char(d, 'Mon')                             AS month_name,
       date_trunc('month', d)::date                  AS month_start,
       date_trunc('week', d)::date                   AS week_start,
       extract(isodow FROM d)::int                   AS weekday_number,
       to_char(d, 'Dy')                              AS weekday_name,
       s.season,
       s.season_order,
       s.season || ' ' || extract(year FROM d)::int  AS season_label,
       (d::date - bounds.latest)                     AS days_from_latest  -- 0 = latest day with data
  FROM bounds
 CROSS JOIN LATERAL generate_series(bounds.d0, bounds.d1, interval '1 day') AS d
  JOIN core.seasons s ON s.month = extract(month FROM d)::int;

ALTER TABLE marts.dim_date ADD PRIMARY KEY (day);
