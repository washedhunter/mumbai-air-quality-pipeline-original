# Design decisions

Plain-English notes on why the project is built the way it is, and what else was considered.

## 1. Data source: OpenAQ only

**Choice:** OpenAQ v3 (API for recent days, its public S3 archive for history).

**Why:** OpenAQ republishes the official CPCB/MPCB/BMC/IITM station data for Mumbai, including the
15-minute readings, and keeps history back to 2019 for the older stations. One source with one
schema keeps the pipeline simple.

**Alternatives considered:**
- *data.gov.in CPCB real-time AQI feed*: only a current snapshot (no history), and getting an API
  key failed. Dropped.
- *WAQI / aqicn*: no history through the API, and its terms forbid redistributing cached data or
  using it for "data analysis" reports. Not used.

## 2. API for recent days, S3 archive for history

The archive has one gzipped CSV per station per day, with the same values and timestamps as the
API (checked on BKC, 2026-11-10: identical first readings). It lags about four days behind real
time, and it needs no key and has no rate limit. The API is real-time but allows 60 requests/minute,
and a day needs one request per sensor (~300). So:
- `auto` (default): API for days less than 6 days old, archive for older days.
- A two-year backfill through the API would take days. Through the archive it takes about an hour.

Both are "public APIs" from OpenAQ; the README says which one each part uses.

## 3. Store raw payloads byte-for-byte

`raw.payloads` keeps the exact response bytes (JSON or gzipped CSV) with the URL, HTTP status,
SHA-256 and fetch time. If a cleaning rule turns out to be wrong, the data can be rebuilt without
calling the API again. 404s (a station with no file that day) are stored too, so coverage gaps are
visible. Each extract first lands in a folder with a manifest; `validate` checks it (status codes,
checksums, parseable, readings actually belong to that day) before anything touches the warehouse.

## 4. One IST day per DAG run, and idempotency

Mumbai's day is an IST day, so a run processes one IST calendar day (the run's logical date). Every
write is scoped to that day and done as delete-then-insert (or upsert) in one transaction:
- raw: upsert on (source, endpoint, location, sensor, day)
- staging: delete the day's rows, insert parsed rows
- core.hourly: rebuild the day (and the day before, see below) from staging
- daily models: rebuild the day before to the day after; aggregate models are rebuilt from the daily
  tables (cheap)

Two subtleties made "re-running gives the same rows" true:
- **Day boundaries.** A reading stamped 00:00 belongs to the previous day's 23:00 hour (stamps are
  the end of the averaging period), and it can come from the next day's file. So each run also
  rebuilds the previous day, and daily models cover day-1 to day+1 (CO/O3 8-hour windows reach back
  7 hours). A test loads the 14 sample days out of order, then in order, and compares checksums of
  every table.
- **Floating-point order.** `avg()` over doubles can change in the last bit when row order changes,
  so averages use exact `numeric` arithmetic and are rounded to 4 decimals.

Backfill runs execute in parallel; advisory locks serialise the per-day cleaning and the SQL model
step so parallel runs can't trip over each other. Prophet's intervals come from random sampling, so
the forecast seeds numpy from the series and its last day: re-running a day reproduces the same
forecast rows too (the idempotency test checksums the forecast values).

A day for which OpenAQ's archive has no file for any station is a gap in the source, not a pipeline
failure: the 404s are stored, the run stays green and the `source_has_data` DQ warning flags it. An
API day with no readings at all fails (and retries), because the API is real-time and that points to
an outage.

## 5. Station → neighbourhood mapping

OpenAQ lists 44 location IDs within 25 km of central Mumbai, but they're only 39 physical stations:
when CPCB moved stations to a new system in 2025, several got new IDs (old `caaqm` ID 2019–2022,
new ID 2025→). `config/stations.csv` merges them into one `station_key` and assigns a neighbourhood
and zone (Mumbai City, Western/Eastern Suburbs, Thane, Navi Mumbai, Mira-Bhayandar). `in_mumbai`
marks the city + suburbs (used for the city AQI); Thane and Navi Mumbai stations are kept for
comparison. A location that isn't in the file shows up as `loc_<id>` / "Unmapped" and a data-quality
warning.

## 6. Units: OpenAQ's "ppb" label on CPCB gases

The newer CPCB stream on OpenAQ labels NO2, SO2 and CO as "ppb". For CO that's clearly wrong: values
are 0.2–1.2, which is CPCB's native mg/m³ (the same stations' older µg/m³ sensor read 500–1,400, i.e.
0.5–1.4 mg/m³). True CO ppb would be in the hundreds. For NO2/SO2, comparing 13 stations' old µg/m³
medians (Nov–Feb 2021/22) with the new "ppb" medians (Nov–Feb 2025/26) showed no consistent factor
(ratios from 0.1 to 13, not the 0.53 a µg→ppb conversion would give), so the label is most likely
the same mislabel of CPCB's native µg/m³. **Default: treat "ppb" as CPCB-native units**
(`PPB_LABEL_MEANS=cpcb_native`). `true_ppb` applies standard conversions (25 °C, 1 atm) if OpenAQ
fixes the data. This is a judgement call and it's documented here and in the README limitations.

## 7. Cleaning rules

Applied per sensor per IST day, so a day's result depends only on that day's readings:
- drop missing values, unknown units, sentinels (±999, 9999…), negatives, zero PM;
- drop physically implausible values (PM ≥ 999 is also excluded by CPCB itself);
- drop **flatlines**: a run of identical values whose first and last readings are ≥ 5 hours apart
  and that has ≥ 4 readings, i.e. about 6 hours of 15-minute data or 6 hourly readings (stuck
  sensors are common in this data, e.g. Kurla SO2 constant at 29.26 for days);
- drop **isolated spikes**: > 3× the rolling median of the 9 surrounding readings (about 2 hours
  for 15-minute sensors, 9 hours for hourly ones) and above it by a pollutant-specific margin.
  A sustained rise (e.g. Diwali night) is kept;
- hourly mean needs ≥ 2 of the 4 quarter-hours (1 for hourly sensors such as the US Consulate);
- several sensors for the same station/pollutant/hour are averaged.
Every dropped row is counted by reason in `ops.cleaning_log`.

## 8. AQI: CPCB National AQI, implemented twice on purpose

`mumbai_aq/aqi.py` is a readable reference implementation with unit tests at every breakpoint edge,
CPCB's worked example (PM2.5 45 → 75), the 16-hour rule, 8-hour windows and the 3-pollutant rule.
The SQL model computes the same thing from `core.aqi_breakpoints`, which is generated from the
Python table, so there's one source of truth for the numbers. Integration tests check that the SQL
sub-index equals the Python one on a grid of ~3,000 values per run, and that every station-day AQI
recomputed in Python from `core.hourly` matches the SQL table.

Choices inside the method:
- Daily value = calendar-day (IST) 24-hour mean, or the max 8-hour rolling mean for CO/O3 (each
  window needs ≥ 6 of 8 hours). A sub-index needs ≥ 16 hours of data that day.
- The Severe band is open-ended in CPCB's table; we use the common convention for its upper
  breakpoint (e.g. PM2.5 500 at 380 µg/m³) and cap at 500.
- Lead (Pb) is left out: no Mumbai station reports it.
- City AQI = mean of the valid station AQIs inside Mumbai that day, only when at least 3 Mumbai
  stations have one (our definition; the median and station count are stored next to it). The
  forecast, the charts and Power BI all use this same series.

## 9. Seasons

IMD's seasons: Winter (Jan–Feb), Summer/pre-monsoon (Mar–May), Monsoon (Jun–Sep), Post-monsoon
(Oct–Dec). In Mumbai, December behaves like winter for pollution; the IMD definition was kept
because it's the standard one, and `core.seasons` is a table, so changing it is one SQL update.

## 10. Forecast: Prophet on log(AQI), with an honest baseline

- **Target:** daily city AQI (and each station with ≥ 180 days that is still reporting).
- **Model:** Prophet with weekly seasonality, plus yearly seasonality once there's a year of data.
  It models log(AQI) because AQI is right-skewed and must stay positive; the 80% interval is
  transformed back and clipped to 0–500.
- **Backtest:** rolling origins (weekly for the city over the last 26 weeks, every 28 days for
  stations), horizons 1–7 days, compared with *last value* and *seasonal naive* (same weekday last
  week). MAE is only computed over cells every model predicted, so the comparison is fair. The
  README reports whatever came out.
- **Backfill runs skip the forecast.** Fitting ~30 models for each of ~740 historical days would add
  hours and nothing useful; the next regular run forecasts from the full history.

Alternatives: SARIMA (needs more tuning per station), gradient boosting with lag features (better
at short horizons but more code and less explainable); Prophet was the stack named in the brief.

## 11. Airflow setup

- Airflow 3.4.0 (current stable), official image, **LocalExecutor** (no Redis/Celery: one machine,
  fits Docker Desktop's memory), separate `api-server`, `scheduler` and `dag-processor`.
- **Two Postgres containers:** Airflow's metadata DB and the warehouse are separate, so Power BI and
  Airflow never share credentials or load.
- **Schedule:** `CronDataIntervalTimetable` at 02:00 IST. A run's logical date is 02:00 IST on the
  day it processes, so backfill dates map 1:1 to days, and the run fires at 02:00 the next day,
  two hours after the day ends, so OpenAQ has received the last readings. (Airflow 3's default
  for cron schedules, `CronTriggerTimetable`, would make the logical date the *firing* time, i.e.
  the next day, which is easy to get off by one.)
- Business logic lives in the `mumbai_aq` package; the DAG file only wires tasks. The same steps run
  without Airflow (`aq local-run`), which is what CI's Postgres job uses.
- The UI runs without a login (`SIMPLE_AUTH_MANAGER_ALL_ADMINS`) because it's a local dev stack; the
  compose file says not to expose it.

## 12. Power BI

Power BI Desktop is Windows-only and can't be driven from here, so the repo ships a build guide,
DAX measures and a theme JSON. Power BI imports the `marts` tables from the warehouse on
`localhost:5433`. Import mode (not DirectQuery) because the tables are small and the SQL models are
rebuilt daily.
