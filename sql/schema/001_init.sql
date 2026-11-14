-- Warehouse schema. Safe to run repeatedly (everything is IF NOT EXISTS / OR REPLACE).
-- raw     : API / archive responses exactly as received
-- staging : one row per reading parsed from raw, no cleaning
-- core    : reference tables + cleaned hourly concentrations (standard units, IST)
-- marts   : SQL models (sql/models) and forecasts, what Power BI reads
-- ops     : pipeline bookkeeping (cleaning log, model runs, data-quality results)

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS marts;
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS raw.payloads (
    payload_id   bigserial PRIMARY KEY,
    source       text        NOT NULL,              -- api | archive | sample
    endpoint     text        NOT NULL,              -- locations | measurements | archive_day
    location_id  bigint,
    sensor_id    bigint,
    target_date  date        NOT NULL,              -- the IST day this request was made for
    url          text        NOT NULL,
    http_status  integer     NOT NULL,
    content_type text,
    body         bytea,                             -- exact bytes received (NULL for 404s)
    sha256       text,
    fetched_at   timestamptz NOT NULL,
    loaded_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT payloads_natural_key
        UNIQUE NULLS NOT DISTINCT (source, endpoint, location_id, sensor_id, target_date)
);
CREATE INDEX IF NOT EXISTS payloads_target_date_idx ON raw.payloads (target_date);

CREATE TABLE IF NOT EXISTS staging.measurements (
    payload_id   bigint           NOT NULL REFERENCES raw.payloads (payload_id) ON DELETE CASCADE,
    target_date  date             NOT NULL,          -- day of the payload it came from
    location_id  bigint           NOT NULL,
    sensor_id    bigint,
    parameter    text             NOT NULL,
    units        text             NOT NULL,
    value        double precision,
    ts_utc       timestamptz      NOT NULL,          -- end of the averaging period
    day_ist      date             NOT NULL           -- IST day of the hour the reading belongs to
);
CREATE INDEX IF NOT EXISTS measurements_day_idx ON staging.measurements (day_ist);
CREATE INDEX IF NOT EXISTS measurements_target_idx ON staging.measurements (target_date);

CREATE TABLE IF NOT EXISTS staging.locations (
    location_id     bigint PRIMARY KEY,
    name            text,
    provider        text,
    lat             double precision,
    lon             double precision,
    datetime_first  timestamptz,
    datetime_last   timestamptz,
    sensors         jsonb,
    discovered_on   date NOT NULL
);

CREATE TABLE IF NOT EXISTS core.stations (
    location_id   bigint PRIMARY KEY,
    station_key   text    NOT NULL,
    station_name  text    NOT NULL,
    operator      text,
    neighbourhood text    NOT NULL,
    zone          text    NOT NULL,
    in_mumbai     boolean NOT NULL,
    provider      text,
    openaq_name   text,
    lat           double precision,
    lon           double precision
);

CREATE TABLE IF NOT EXISTS core.aqi_breakpoints (
    pollutant text    NOT NULL,
    band      integer NOT NULL,
    b_lo      double precision NOT NULL,
    b_hi      double precision NOT NULL,
    i_lo      integer NOT NULL,
    i_hi      integer NOT NULL,
    category  text    NOT NULL,
    PRIMARY KEY (pollutant, band)
);

-- IMD seasons: winter Jan-Feb, pre-monsoon (summer) Mar-May, monsoon Jun-Sep, post-monsoon Oct-Dec.
CREATE TABLE IF NOT EXISTS core.seasons (
    month       integer PRIMARY KEY CHECK (month BETWEEN 1 AND 12),
    season      text    NOT NULL,
    season_order integer NOT NULL
);

CREATE TABLE IF NOT EXISTS core.hourly (
    station_key  text             NOT NULL,
    parameter    text             NOT NULL,
    hour_ist     timestamp        NOT NULL,          -- hour start, local IST wall-clock time
    day_ist      date             NOT NULL,
    value        double precision NOT NULL,          -- µg/m³ (CO: mg/m³)
    n_readings   integer          NOT NULL,
    n_sensors    integer          NOT NULL,
    PRIMARY KEY (station_key, parameter, hour_ist)
);
CREATE INDEX IF NOT EXISTS hourly_day_idx ON core.hourly (day_ist);

CREATE TABLE IF NOT EXISTS marts.forecasts (
    run_date      date             NOT NULL,         -- last day of data the model saw
    level         text             NOT NULL,         -- city | station
    station_key   text             NOT NULL,         -- 'mumbai' for the city series
    target_date   date             NOT NULL,
    horizon_days  integer          NOT NULL,
    aqi_forecast  double precision NOT NULL,
    aqi_lower     double precision NOT NULL,         -- 80% interval
    aqi_upper     double precision NOT NULL,
    model         text             NOT NULL,
    created_at    timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (run_date, level, station_key, target_date)
);

CREATE TABLE IF NOT EXISTS marts.forecast_backtest (
    run_date      date             NOT NULL,         -- pipeline run that produced the backtest
    level         text             NOT NULL,
    station_key   text             NOT NULL,
    origin_date   date             NOT NULL,         -- last day of training data
    target_date   date             NOT NULL,
    horizon_days  integer          NOT NULL,
    model         text             NOT NULL,         -- prophet | last_value | seasonal_naive_7d
    actual        double precision NOT NULL,
    predicted     double precision NOT NULL,
    PRIMARY KEY (run_date, level, station_key, origin_date, target_date, model)
);

CREATE TABLE IF NOT EXISTS ops.cleaning_log (
    day_ist    date    NOT NULL,
    parameter  text    NOT NULL,
    reason     text    NOT NULL,
    n_rows     integer NOT NULL,
    PRIMARY KEY (day_ist, parameter, reason)
);

CREATE TABLE IF NOT EXISTS ops.model_runs (
    run_at      timestamptz NOT NULL DEFAULT now(),
    model       text        NOT NULL,
    seconds     double precision NOT NULL,
    row_count   bigint
);

CREATE TABLE IF NOT EXISTS ops.dq_results (
    run_date   date        NOT NULL,
    check_name text        NOT NULL,
    severity   text        NOT NULL,                 -- error | warn
    passed     boolean     NOT NULL,
    observed   text,
    detail     text,
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_date, check_name)
);

-- CPCB sub-index from the breakpoint table (mirrors mumbai_aq.aqi.sub_index).
CREATE OR REPLACE FUNCTION core.aqi_subindex(p_pollutant text, p_conc double precision)
RETURNS double precision
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN p_conc IS NULL OR p_conc < 0 THEN NULL
        ELSE COALESCE(
            (SELECT GREATEST(
                        (b.i_hi - b.i_lo) / (b.b_hi - b.b_lo) * (p_conc - b.b_lo) + b.i_lo,
                        CASE WHEN b.i_lo > 0 THEN b.i_lo - 1 ELSE 0 END)
               FROM core.aqi_breakpoints b
              WHERE b.pollutant = p_pollutant AND p_conc <= b.b_hi
              ORDER BY b.band
              LIMIT 1),
            CASE WHEN EXISTS (SELECT 1 FROM core.aqi_breakpoints WHERE pollutant = p_pollutant)
                 THEN 500.0 END)
    END
$$;

-- Round half up, like mumbai_aq.aqi.round_aqi.
CREATE OR REPLACE FUNCTION core.round_aqi(v double precision)
RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT floor(v + 0.5)::integer $$;

CREATE OR REPLACE FUNCTION core.aqi_category(v double precision)
RETURNS text LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN v IS NULL THEN NULL ELSE
        (SELECT category FROM core.aqi_breakpoints
          WHERE pollutant = 'pm25' AND core.round_aqi(v) <= i_hi ORDER BY band LIMIT 1)
    END
$$;
