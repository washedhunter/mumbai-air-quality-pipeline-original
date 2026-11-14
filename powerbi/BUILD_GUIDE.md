# Power BI build guide

This guide builds the five-page **Mumbai Air Quality** report on top of the warehouse that the
pipeline fills. Follow it top to bottom; every table, column and measure named here exists in the
warehouse (checked with `uv run python scripts/check_powerbi_guide.py`, see the end).

Time needed: about 1.5–2 hours the first time.

---

## 0. Before you start

1. **Power BI Desktop** (free, Windows): Microsoft Store, or
   `winget install --id 9NTXR16HNW1T -e --source msstore`.
2. The pipeline stack is running and has data:
   ```
   docker compose up -d --wait
   docker compose run --rm pipeline status 2026-11-01 2026-11-13
   ```
   The warehouse listens on **localhost:5433**, database **air_quality**, user **aq**, password
   **aq** (or whatever `WAREHOUSE_PASSWORD` is in your `.env`).
3. Download the files in this folder: `theme.json` and `measures.dax`.

---

## 1. Connect to PostgreSQL

1. Power BI Desktop → **Home → Get data → More… → Database → PostgreSQL database → Connect**.
2. **Server:** `localhost:5433` **Database:** `air_quality`
   **Data Connectivity mode:** **Import** (the tables are small and change once a day).
   Leave *Advanced options* empty → **OK**.
3. Credentials: choose the **Database** tab, User name `aq`, Password `aq` → **Connect**.
   If it asks about encryption, choose **OK** to connect without encryption (local Docker
   container; nothing leaves your machine).
   - If you get "The remote certificate is invalid" → **File → Options and settings → Data source
     settings → localhost:5433;air_quality → Edit Permissions → untick "Encrypt connections"**.
   - Older Power BI versions may ask to install the Npgsql provider; current versions ship it.

## 2. Pick the tables

In the Navigator, tick these 15 items in the **marts** schema (they appear as `marts.<name>`):

| Navigator name | What it is |
|---|---|
| `marts.dim_date` | Calendar: day, month, week, season (IMD) |
| `marts.dim_station` | One row per station: name, operator, neighbourhood, zone, lat/lon, in_mumbai |
| `marts.dim_pollutant` | Display names (PM2.5, NO₂…), units, averaging, sort order |
| `marts.dim_aqi_category` | CPCB categories with official colours and health impact |
| `marts.city_daily_aqi` | Mumbai AQI per day (mean of Mumbai stations), median, min/max, station count |
| `marts.daily_station_aqi` | AQI per station per day, category, dominant pollutant |
| `marts.daily_pollutant` | Concentration and sub-index per station, day and pollutant |
| `marts.weekly_trends` | Weekly averages per station and for the city (`station_key = 'mumbai'`) |
| `marts.seasonal_patterns` | Mean / p90 per season and year, per neighbourhood and for the city |
| `marts.neighbourhood_pollutant` | Neighbourhood × pollutant × month, with rank and ratio to average |
| `marts.dominant_pollutant` | Days each pollutant set the AQI, per station and month |
| `marts.station_coverage` | Data coverage per station |
| `marts.latest_forecast` | Newest 7-day forecast (city + stations) with 80% interval |
| `marts.backtest_summary` | Backtest MAE per level, model and horizon |
| `marts.forecast_backtest` | Every backtest prediction (for drill-down) |

Click **Transform Data** (not Load).

## 3. Power Query clean-up

For each query on the left:
1. **Rename** it without the `marts ` prefix (right-click → Rename): e.g. `marts dim_date` →
   `dim_date`. The measures assume these exact names.
2. Check the **data types** (icon left of each column header):
   - Every `day`, `month_start`, `week_start`, `target_date`, `run_date`, `origin_date`,
     `first_day`, `last_day` column → **Date**.
   - `aqi`, `aqi_forecast`, `aqi_lower`, `aqi_upper`, `concentration`, `sub_index`, `value`,
     `mean_*`, `p90_value`, `pct_*`, `mae`, `ratio_to_average`, `lat`, `lon` → **Decimal number**.
   - Counts (`n_stations`, `days`, `hours_with_data`, `horizon_days`, `rank_in_month`, `sort_order`,
     `season_order`, `days_dominant`, `n`) → **Whole number**.
3. In `dim_station`, set `lat` and `lon` **Data category** later in Model view (step 4.3).

**Home → Close & Apply.**

## 4. Model

### 4.1 Mark the date table
Select `dim_date` → **Table tools → Mark as date table** → Date column: `day`.

### 4.2 Relationships
Go to **Model view**. Delete any relationships Power BI auto-created, then create these (drag
the column from the fact table onto the dimension column). All are **Many-to-one (*:1), single
cross-filter direction, active**:

| From (many) | To (one) |
|---|---|
| `city_daily_aqi[day]` | `dim_date[day]` |
| `daily_station_aqi[day]` | `dim_date[day]` |
| `daily_station_aqi[station_key]` | `dim_station[station_key]` |
| `daily_station_aqi[dominant_pollutant]` | `dim_pollutant[pollutant]` |
| `daily_station_aqi[category]` | `dim_aqi_category[category]` |
| `daily_pollutant[day]` | `dim_date[day]` |
| `daily_pollutant[station_key]` | `dim_station[station_key]` |
| `daily_pollutant[pollutant]` | `dim_pollutant[pollutant]` |
| `neighbourhood_pollutant[pollutant]` | `dim_pollutant[pollutant]` |
| `neighbourhood_pollutant[month_start]` | `dim_date[day]` |
| `seasonal_patterns[pollutant]` | `dim_pollutant[pollutant]` |
| `dominant_pollutant[station_key]` | `dim_station[station_key]` |
| `dominant_pollutant[pollutant]` | `dim_pollutant[pollutant]` |
| `station_coverage[station_key]` | `dim_station[station_key]` |
| `latest_forecast[target_date]` | `dim_date[day]` |

Leave `weekly_trends`, `backtest_summary` and `forecast_backtest` unrelated (they're filtered
inside their own visuals). `city_daily_aqi[category]` stays unrelated too (only one relationship
from category is allowed without ambiguity).

### 4.3 Column settings
- `dim_station[lat]` → **Column tools → Data category → Latitude**; `dim_station[lon]` → **Longitude**.
- `dim_date[month_name]` → **Sort by column** → `month`.
- `dim_date[weekday_name]` → **Sort by column** → `weekday_number`.
- `dim_date[season]` and `seasonal_patterns[season]` → **Sort by column** → `season_order`.
- `dim_pollutant[display_name]` → **Sort by column** → `sort_order`.
- `dim_aqi_category[category]` → **Sort by column** → `sort_order`.
- Hide (right-click → Hide in report view) every `station_key`, `pollutant` and `day` column on
  the fact tables, so people use the dimension columns instead.

### 4.4 Measures
1. **Home → Enter data** → name the table `_Measures` → **Load**. (Delete its empty `Column1`
   after the first measure exists.)
2. Open `measures.dax`. For each block (`Name = expression`), select `_Measures` →
   **Modeling → New measure**, paste the block, press Enter.
3. Formats (select the measure → Measure tools):
   - `% Days Poor or Worse`, `% Days Good or Satisfactory`, `Station Days Poor or Worse %`,
     `Dominant Share`, `Prophet vs Best Baseline` → **Percentage, 1 decimal**.
   - All AQI / MAE measures → **Decimal, 0 decimals**; `Avg Concentration`, `Max Concentration`
     → 1 decimal.
   - `Latest Day`, `Forecast Run Date` → Date, format `dd mmm yyyy`.

### 4.5 Theme
**View → Themes → Browse for themes →** `theme.json`. It sets the colours, fonts and the light-grey
page background.

---

## 5. Pages

Canvas: **View → Page view → Fit to page**, page size **16:9** (default). Positions below are rough
(left→right, top→bottom); exact pixels don't matter.

For every page, add a **title text box** at the top-left with the page name, and a small text box
at the bottom: *"Source: OpenAQ (CPCB/MPCB/BMC/IITM stations). AQI: CPCB National AQI. Last data:"*
followed by a card of `[Latest Day]`.

### Page 1 — Overview

| # | Visual | Fields | Settings |
|---|---|---|---|
| 1 | **Slicer** (top-right) | `dim_date[day]` | Style **Between**. Set the range to the last 12 months. |
| 2 | **Card** | `Latest City AQI` | Callout value → *Conditional formatting → fx → Format style: Field value → `Latest AQI Colour`* on the card **background**. Title "Mumbai AQI (latest day)". |
| 3 | **Card** | `Latest City Category` | Title "Category". |
| 4 | **Card** | `Latest Dominant Pollutant` | Title "Dominant pollutant". |
| 5 | **Card** | `Stations Reporting (Latest Day)` | Title "Stations reporting". |
| 6 | **Card** | `% Days Poor or Worse` | Title "Days Poor or worse (selected period)". |
| 7 | **Line chart** (wide, middle) | X: `dim_date[day]`; Y: `City AQI`, `City AQI 7d Rolling` | Analytics pane → **Constant line** at 100 ("Satisfactory limit") and 200 ("Moderate limit"), dashed. Title "Mumbai daily AQI". |
| 8 | **Stacked column chart** (bottom-left) | X: `dim_date[month_start]` (turn off date hierarchy: click the arrow → `month_start`); Y: `Days With City AQI`; Legend: `city_daily_aqi[category]` | Format → Columns → set each category's colour to the CPCB colour from `dim_aqi_category` (Good #00B050, Satisfactory #92D050, Moderate #FFFF00, Poor #FF9900, Very Poor #FF0000, Severe #C00000). Title "Days per category, by month". |
| 9 | **Map** (bottom-right; use *Azure Map* if *Map* is disabled in your tenant) | Latitude: `dim_station[lat]`; Longitude: `dim_station[lon]`; Bubble size: `Station AQI`; Tooltips: `dim_station[station_name]`, `dim_station[neighbourhood]`, `Station AQI` | Bubbles → Colors → **fx** → Field value → `AQI Colour`. Title "Station AQI (selected period)". |

### Page 2 — Neighbourhoods

| # | Visual | Fields | Settings |
|---|---|---|---|
| 1 | **Slicer** | `dim_date[day]` | Between; sync with page 1 (**View → Sync slicers**). |
| 2 | **Slicer** | `dim_station[zone]` | Dropdown, multi-select. |
| 3 | **Clustered bar chart** (left) | Y: `dim_station[neighbourhood]`; X: `Station AQI` | Sort descending by `Station AQI`. Bars → **fx** → Field value → `AQI Colour`. Title "Average AQI by neighbourhood". |
| 4 | **Matrix** (right, heatmap) | Rows: `dim_station[neighbourhood]`; Columns: `dim_pollutant[display_name]`; Values: `Avg Sub-index` | Filter pane (this visual): `dim_pollutant[pollutant]` is not `aqi`. Cell elements → **Background color → fx → Gradient**: min #00B050, center 100 #FFFF00, max 300 #FF0000. Title "Sub-index by neighbourhood and pollutant (100 = Satisfactory limit)". |
| 5 | **Line chart** (bottom) | X: `dim_date[week_start]`; Y: `Station AQI`; Legend: `dim_station[neighbourhood]` | Filter pane: `dim_station[in_mumbai]` = True. Title "Weekly AQI by neighbourhood (Mumbai)". |
| 6 | **Table** (bottom-right) | `station_coverage[station_name]`, `[neighbourhood]`, `[first_day]`, `[last_day]`, `[days_with_aqi]`, `[pct_days_with_aqi]`, `[pollutants]` | Title "Station coverage". |

### Page 3 — Pollutants

| # | Visual | Fields | Settings |
|---|---|---|---|
| 1 | **Slicer** | `dim_pollutant[display_name]` | **Single select**; filter out `AQI`. Default PM2.5. |
| 2 | **Slicer** | `dim_date[day]` | Between (synced). |
| 3 | **Card** | `Avg Concentration` | Title "Average concentration". Add a second card next to it with `Concentration Unit` (no title). |
| 4 | **Card** | `Max Concentration` | Title "Highest daily value". |
| 5 | **Line chart** (wide) | X: `dim_date[day]`; Y: `Avg Concentration`; Legend: `dim_station[zone]` | Title "Daily concentration by zone". |
| 6 | **Clustered column chart** | X: `dim_pollutant[display_name]`; Y: `Dominant Share` | **Edit interactions**: make slicer 1 *not* filter this visual (so all pollutants show). Title "How often each pollutant sets the AQI". |
| 7 | **Matrix** | Rows: `dim_station[station_name]`; Columns: `dim_date[season]`; Values: `Avg Concentration` | Background color gradient (low #FFFFFF → high #C73E1D). Title "Concentration by station and season". |

### Page 4 — Seasonal

| # | Visual | Fields | Settings |
|---|---|---|---|
| 1 | **Slicer** | `dim_pollutant[display_name]` | Single select; default **AQI**. |
| 2 | **Clustered column chart** | X: `seasonal_patterns[season]`; Y: `seasonal_patterns[mean_value]` (Average); Legend: `seasonal_patterns[year]` | Filter pane: `seasonal_patterns[level]` = city. Title "Mumbai by season and year". |
| 3 | **Matrix** | Rows: `seasonal_patterns[area]`; Columns: `seasonal_patterns[season]`; Values: `seasonal_patterns[mean_value]` (Average) | Filter: `level` = neighbourhood. Background gradient #00B050 → #FFFF00 → #FF0000. Title "Neighbourhoods by season". |
| 4 | **Line chart** | X: `dim_date[month_name]`; Y: `City AQI`; Legend: `dim_date[year]` | Title "Monthly profile of Mumbai AQI". (Not affected by slicer 1 — edit interactions.) |
| 5 | **Table** | `seasonal_patterns[season_label]`, `[days]`, `[mean_value]`, `[p90_value]`, `[pct_poor_or_worse]` | Filter: `level` = city, `pollutant` = aqi. Sort by `season_order` then `year`. Title "Season summary (city AQI)". |

### Page 5 — Forecast

| # | Visual | Fields | Settings |
|---|---|---|---|
| 1 | **Card** | `Forecast Run Date` | Title "Forecast made with data up to". |
| 2 | **Line chart** (wide) | X: `dim_date[day]`; Y: `City AQI` and `Forecast AQI` | Filter pane (this visual): `latest_forecast[level]` = city, and `dim_date[days_from_latest]` **is greater than or equal to** -60 **and is less than or equal to** 7 (Advanced filtering). `days_from_latest` is 0 on the latest day with data, so this window moves by itself every day. Analytics pane → **Error bars** on `Forecast AQI`: Upper bound `Forecast Upper 80`, Lower bound `Forecast Lower 80`, style *Band*. Title "City AQI: last 60 days and 7-day forecast (80% interval)". |
| 3 | **Table** | `latest_forecast[target_date]`, `[aqi_forecast]`, `[aqi_lower]`, `[aqi_upper]`, `[category_forecast]` | Filter: `level` = city. Conditional formatting on `aqi_forecast` background → rules by value (0–50 #00B050, 51–100 #92D050, 101–200 #FFFF00, 201–300 #FF9900, 301–400 #FF0000, 401–500 #C00000). Title "Next 7 days". |
| 4 | **Cards ×3** | `Prophet MAE (city)`, `Seasonal Naive MAE (city)`, `Last Value MAE (city)` | Titles "Prophet MAE", "Seasonal naive MAE", "Last value MAE" (AQI points). |
| 5 | **Clustered column chart** | X: `backtest_summary[horizon_days]`; Y: `backtest_summary[mae]` (Average); Legend: `backtest_summary[model]` | Filter: `level` = city. Title "Backtest: mean absolute error by days ahead". |
| 6 | **Table** (small) | `latest_forecast[station_key]`, `[target_date]`, `[aqi_forecast]` | Filter: `level` = station, `horizon_days` = 1. Title "Tomorrow, per station". |
| 7 | **Text box** | — | "Forecast: Prophet on log(AQI) with weekly and yearly seasonality. Backtest: rolling origins over the last 26 weeks, compared with naive baselines. See the README for the numbers and caveats." |

---

## 6. Save, export, screenshots

1. **File → Save as** → `powerbi/mumbai_air_quality.pbix`.
2. **File → Save as** → type **Power BI project (.pbip)** → `powerbi/mumbai_air_quality.pbip`
   (creates a folder with the report and semantic model as text, which diffs well in git).
   If .pbip isn't offered: **File → Options and settings → Options → Preview features → Power BI
   Project (.pbip) save option**, restart.
3. **File → Export → Export to PDF** → `powerbi/mumbai_air_quality.pdf`.
4. Screenshots: on each page press **Win + Shift + S**, save as
   `docs/img/powerbi_overview.png`, `powerbi_neighbourhoods.png`, `powerbi_pollutants.png`,
   `powerbi_seasonal.png`, `powerbi_forecast.png`.
5. Commit them (the `.gitignore` already skips the .pbip local cache files).

## 7. Refreshing

After the pipeline runs (every day at 02:00 IST once the DAG is unpaused), click
**Home → Refresh**. The SQL models are rebuilt in a single transaction, so a refresh never sees a
half-built set of tables.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Couldn't connect" | `docker compose ps` should show `warehouse` healthy and port `0.0.0.0:5433->5432`. Port 5433 taken? Set `WAREHOUSE_PORT=5434` in `.env`, `docker compose up -d`, connect to `localhost:5434`. |
| Tables empty | Run a day first: `docker compose run --rm pipeline run-day <yesterday>`. |
| A visual shows one number for every row | A relationship is missing or points the wrong way — compare with 4.2. |
| Map visual is greyed out | Your organisation disabled map visuals: **File → Options → Security → Map and filled map visuals** (personal accounts), or use a scatter chart of lon (X) / lat (Y). |
| Measure error "cannot find table" | The query wasn't renamed in step 3 (still `marts dim_date`). |
