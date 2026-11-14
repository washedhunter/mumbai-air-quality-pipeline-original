# Power BI build guide

This guide builds the five-page **Mumbai Air Quality** report on top of the warehouse that the
pipeline fills. Follow it top to bottom; every table, column and measure named here exists in the
warehouse (checked with `uv run python scripts/check_powerbi_guide.py`, see the end).

Time needed: about 1.5–2 hours the first time.

Last checked end to end with Power BI Desktop (Microsoft Store build, November 2026; reports save
in the PBIR format). The built result is in this folder: `mumbai_air_quality.pbix`, the same report
as a Power BI project (`mumbai_air_quality.pbip` + the `.Report` / `.SemanticModel` folders) and
`mumbai_air_quality.pdf`. Menu names move between Power BI releases; where this version differs from
older ones the guide says so.

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

1. Power BI Desktop → **Home → Get data → More… → Database → PostgreSQL database → Connect**
   (or type *PostgreSQL* in the Get Data search box). If a pop-up offers the "new Get Data
   experience", either works; **No thanks** gives the dialog described here.
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
1. **Rename** it without the `marts ` prefix (double-click the name, or right-click → Rename): e.g.
   `marts dim_date` → `dim_date`. The measures assume these exact names.
2. Check the **data types** (icon left of each column header). The PostgreSQL connector brings the
   warehouse types across, so normally nothing needs changing:
   - Every `day`, `month_start`, `week_start`, `target_date`, `run_date`, `origin_date`,
     `first_day`, `last_day` column → **Date**.
   - `aqi`, `aqi_forecast`, `aqi_lower`, `aqi_upper`, `concentration`, `sub_index`, `value`,
     `mean_*`, `p90_value`, `pct_*`, `mae`, `ratio_to_average`, `lat`, `lon` → **Decimal number**.
     Exception: `daily_station_aqi[aqi]` is a whole number (a station's AQI is an integer); leave it.
   - Counts (`n_stations`, `days`, `hours_with_data`, `horizon_days`, `rank_in_month`, `sort_order`,
     `season_order`, `days_dominant`, `n`) → **Whole number**.
3. In `dim_station`, set `lat` and `lon` **Data category** later (step 4.3).

**Home → Close & Apply.** Don't load while the pipeline is rebuilding the marts (a backfill or the
daily run): the load fails with *"PostgreSQL: Exception while reading from stream"*. Wait for the run
to finish, then **Apply changes** (yellow bar) or **Home → Refresh**.

## 4. Model

### 4.1 Mark the date table
**Table view** → select `dim_date` → **Table tools → Mark as date table** → switch it **On** →
date column `day` → **Save**.

### 4.2 Relationships
Power BI auto-detects about a dozen relationships on load and some are wrong (for example
`dim_aqi_category[sort_order]` → `dim_pollutant[sort_order]`). **Home → Manage relationships** →
tick the box in the header row to select all → **Delete** → confirm. Then create these. All are
**Many-to-one (*:1), single cross-filter direction, active**:

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

Two ways to create them:
- **Drag and drop** in Model view (fact column onto the dimension column), or **Manage
  relationships → New relationship** and pick the two tables and columns.
- **Faster: TMDL view** (left rail, the **TMDL** icon). Open `relationships.tmdl` from this folder,
  paste the whole file into the editor, click **Apply**, then **Refresh now** on the yellow bar.
  It creates exactly the 15 relationships above.

Leave `weekly_trends`, `backtest_summary` and `forecast_backtest` unrelated (they're filtered
inside their own visuals). `city_daily_aqi[category]` stays unrelated too (only one relationship
from category is allowed without ambiguity).

### 4.3 Column settings
- `dim_station[lat]` → **Column tools → Data category → Latitude**; `dim_station[lon]` → **Longitude**.
  Set **Summarization → Don't summarize** on both too, so maps and scatter charts get one point
  per station.
- `dim_date[month_name]` → **Sort by column** → `month`.
- `dim_date[weekday_name]` → **Sort by column** → `weekday_number`.
- `dim_date[season]` and `seasonal_patterns[season]` → **Sort by column** → `season_order`.
- `dim_pollutant[display_name]` → **Sort by column** → `sort_order`.
- `dim_aqi_category[category]` → **Sort by column** → `sort_order`.
- Hide the fact-table key columns so people use the dimension columns instead:
  `city_daily_aqi[day]`, `daily_station_aqi[day]`, `daily_station_aqi[station_key]`,
  `daily_pollutant[day]`, `daily_pollutant[station_key]`, `daily_pollutant[pollutant]`,
  `dominant_pollutant[station_key]`, `dominant_pollutant[pollutant]`,
  `neighbourhood_pollutant[pollutant]`, `station_coverage[station_key]`. Quickest in **Model
  view**: Ctrl+click them in the Data pane → Properties → **Is hidden** on (or right-click → Hide in
  report view). Leave `seasonal_patterns[pollutant]` and `latest_forecast[station_key]` visible:
  page 4 filters on the first and page 5 shows the second.

### 4.4 Measures
1. **Home → Enter data** → name the table `_Measures` → **Load**. (Delete its empty `Column1`
   after the first measure exists.)
2. Open `measures.dax`. For each block (`Name = expression`), select `_Measures` →
   **Modeling → New measure**, paste the block, press Enter. (TMDL view also works: wrap the
   measures in `createOrReplace` / `ref table _Measures` and indent each expression under a
   `measure 'Name' =` line; the editor shows "cannot find name" warnings until **Apply**, which is
   expected because the measures reference each other.)
3. Formats (select the measure → Measure tools):
   - `% Days Poor or Worse`, `% Days Good or Satisfactory`, `Station Days Poor or Worse %`,
     `Dominant Share`, `Prophet vs Best Baseline` → **Percentage, 1 decimal**.
   - All AQI / MAE measures → **Decimal, 0 decimals**; `Avg Concentration`, `Max Concentration`
     → 1 decimal.
   - `Latest Day`, `Forecast Run Date` → Date, format `dd mmm yyyy`.

### 4.5 Theme
**Design → Import theme →** `theme.json` (older versions: **View → Themes → Browse for themes**).
It sets the colours, fonts and the light-grey page background.

---

## 5. Pages

Canvas: **View → Page view → Fit to page**, page size **16:9**. New reports now default to
1920 × 1080; the built report uses 1280 × 720 (**Format page → Canvas settings**), which keeps text
readable in screenshots. Positions below are rough (left→right, top→bottom); exact pixels don't
matter.

For every page, add a **title text box** at the top-left with the page name, and a small text box
at the bottom: *"Source: OpenAQ (CPCB/MPCB/BMC/IITM stations). AQI: CPCB National AQI. Last data:"*
followed by a card of `[Latest Day]`.

Tips that apply to every page:
- Visuals get an automatic subtitle ("City AQI by day"…) in this version's default theme; turn it
  off under **Format visual → General → Title → Subtitle**.
- On cards, turn off **Category label** when the visual title already says what the number is.
- On slicers that have a title, turn off **Slicer header** so the name doesn't appear twice.
- A slicer's saved selection is what readers see first, so set it (dates, PM2.5, AQI…) before
  saving.

### Page 1 — Overview

| # | Visual | Fields | Settings |
|---|---|---|---|
| 1 | **Slicer** (top-right) | `dim_date[day]` | Style **Between** (the default for a date). Drag the handles, or type the two dates, to the last 12 months before saving. |
| 2 | **Card** | `Latest City AQI` | Format visual → **General → Effects → Background → fx** (conditional formatting button) → Format style *Field value* → `Latest AQI Colour`. Title "Mumbai AQI (latest day)". |
| 3 | **Card** | `Latest City Category` | Title "Category". |
| 4 | **Card** | `Latest Dominant Pollutant` | Title "Dominant pollutant". |
| 5 | **Card** | `Mumbai Stations (Latest Day)` | Title "Mumbai stations in the city AQI". |
| 6 | **Card** | `% Days Poor or Worse` | Title "Days Poor or worse (selected period)". |
| 7 | **Line chart** (wide, middle) | X: `dim_date[day]`; Y: `City AQI`, `City AQI 7d Rolling` | Analytics pane → **Y-Axis Constant Line** (called *Constant line* in older versions) at 100 ("Satisfactory limit") and 200 ("Moderate limit"), dashed. Title "Mumbai daily AQI". |
| 8 | **Stacked column chart** (bottom-left) | X: `dim_date[month_start]` (no date hierarchy appears once `dim_date` is marked as the date table; if one does, click the arrow → `month_start`); Y: `Days With City AQI`; Legend: `city_daily_aqi[category]` | Format → Columns → set each category's colour to the CPCB colour from `dim_aqi_category` (Good #00B050, Satisfactory #92D050, Moderate #FFFF00, Poor #FF9900, Very Poor #FF0000, Severe #C00000). Title "Days per category, by month". |
| 9 | **Map** (bottom-right) | Latitude: `dim_station[lat]`; Longitude: `dim_station[lon]`; Bubble size: `Station AQI`; Tooltips: `dim_station[station_name]`, `dim_station[neighbourhood]`, `Station AQI` | Bubbles → Colors → **fx** → Field value → `AQI Colour`. Title "Station AQI (selected period)". **Note:** current Power BI Desktop ships with *Map* turned off (**File → Options and settings → Options → Global → Security → Map and filled map visuals**) and *Azure Map* asks you to sign in. The built report uses the no-sign-in fallback instead: **Scatter chart** with Values `dim_station[station_name]`, X `dim_station[lon]` (Average), Y `dim_station[lat]` (Average), Size `Station AQI`, Tooltips `dim_station[neighbourhood]`; **Markers → Colors → fx → Field value →** `AQI Colour`. |

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
| 1 | **Slicer** | `dim_pollutant[display_name]` | Style **Dropdown**, **Single select** (Format → Slicer settings → Options / Selection). Filter pane: `display_name` is not **AQI**. Click **PM2.5** so it's selected when you save; the saved selection is the default. |
| 2 | **Slicer** | `dim_date[day]` | Between (synced). |
| 3 | **Card** | `Avg Concentration` | Title "Average concentration". Add a second card next to it with `Concentration Unit` (no title). |
| 4 | **Card** | `Max Concentration` | Title "Highest daily value". |
| 5 | **Line chart** (wide) | X: `dim_date[day]`; Y: `Avg Concentration`; Legend: `dim_station[zone]` | Title "Daily concentration by zone". |
| 6 | **Clustered column chart** | X: `dim_pollutant[display_name]`; Y: `Dominant Share` | **Edit interactions**: make slicer 1 *not* filter this visual (so all pollutants show). Title "How often each pollutant sets the AQI". |
| 7 | **Matrix** | Rows: `dim_station[station_name]`; Columns: `dim_date[season]`; Values: `Avg Concentration` | Background color gradient (low #FFFFFF → high #C73E1D). Title "Concentration by station and season". |

### Page 4 — Seasonal

| # | Visual | Fields | Settings |
|---|---|---|---|
| 1 | **Slicer** | `dim_pollutant[display_name]` | Dropdown, single select. Click **AQI** before saving (the saved selection is the default). |
| 2 | **Clustered column chart** | X: `seasonal_patterns[season]`; Y: `seasonal_patterns[mean_value]` (Average); Legend: `seasonal_patterns[year]` | Filter pane: `seasonal_patterns[level]` = city. Title "Mumbai by season and year". |
| 3 | **Matrix** | Rows: `seasonal_patterns[area]`; Columns: `seasonal_patterns[season]`; Values: `seasonal_patterns[mean_value]` (Average) | Filter: `level` = neighbourhood. Background gradient #00B050 → #FFFF00 → #FF0000. Title "Neighbourhoods by season". |
| 4 | **Line chart** | X: `dim_date[month_name]`; Y: `City AQI`; Legend: `dim_date[year]` | Title "Monthly profile of Mumbai AQI". (Not affected by slicer 1 — edit interactions.) |
| 5 | **Table** | `seasonal_patterns[year]`, `seasonal_patterns[season_order]`, `seasonal_patterns[season_label]`, `[days]`, `[mean_value]`, `[p90_value]`, `[pct_poor_or_worse]` | Filter: `level` = city, `pollutant` = aqi. Click the `year` header to sort, then **Shift+click** the `season_order` header to add it as a second sort key, so rows run in time order (Winter, Summer, Monsoon, Post-monsoon per year). **Edit interactions**: slicer 1 doesn't filter this table either (it is always the city AQI). Title "Season summary (city AQI)". |

### Page 5 — Forecast

| # | Visual | Fields | Settings |
|---|---|---|---|
| 1 | **Card** | `Forecast Run Date` | Title "Forecast made with data up to". |
| 2 | **Line chart** (wide) | X: `dim_date[day]`; Y: `City AQI` and `Forecast AQI` | Filter pane (this visual): `latest_forecast[level]` = city, and `dim_date[days_from_latest]` **is greater than or equal to** -60 **and is less than or equal to** 7 (Advanced filtering). `days_from_latest` is 0 on the latest day with data, so this window moves by itself every day. Analytics pane → **Error bars**: Series `Forecast AQI` → **Enabled** on → Upper bound **+ Add data** → `Forecast Upper 80`, Lower bound → `Forecast Lower 80` (Relationship to measure: Absolute) → turn **Bar** and **Markers** off and **Error band** on, which draws the shaded interval. Title "City AQI: last 60 days and 7-day forecast (80% interval)". |
| 3 | **Table** | `latest_forecast[target_date]`, `[aqi_forecast]`, `[aqi_lower]`, `[aqi_upper]`, `[category_forecast]` | Filter: `level` = city. Conditional formatting on `aqi_forecast` background → Format style **Rules**, based on field **Sum of aqi_forecast** (one row per day, so the sum is the value) (0–50 #00B050, 51–100 #92D050, 101–200 #FFFF00, 201–300 #FF9900, 301–400 #FF0000, 401–500 #C00000). Turn **Totals** off. Optional: set `latest_forecast[target_date]` format to `dd mmm yyyy` (Column tools) so the columns fit. Title "Next 7 days". |
| 4 | **Cards ×3** | `Prophet MAE (city)`, `Seasonal Naive MAE (city)`, `Last Value MAE (city)` | Titles "Prophet MAE (AQI points)", "Seasonal naive MAE (AQI points)", "Last value MAE (AQI points)". |
| 5 | **Clustered column chart** | X: `backtest_summary[horizon_days]`; Y: `backtest_summary[mae]` (Average); Legend: `backtest_summary[model]` | Filter: `level` = city. Title "Backtest: mean absolute error by days ahead". |
| 6 | **Table** (small) | `latest_forecast[station_key]`, `[target_date]`, `[aqi_forecast]` | Filter: `level` = station, `horizon_days` = 1. Totals off. Title "Tomorrow, per station". |
| 7 | **Text box** | — | "Forecast: Prophet on log(AQI) with weekly and yearly seasonality. Backtest: rolling origins over the last 26 weeks, compared with naive baselines. See the README for the numbers and caveats." |

---

## 6. Save, export, screenshots

1. **File → Save as → Browse this device** → `powerbi/mumbai_air_quality.pbix` (the quick "Save
   this file" box defaults to OneDrive; use **More options…** to pick the repo folder).
2. **File → Save as → Browse this device** → Save as type **Power BI project files (*.pbip)** →
   `mumbai_air_quality.pbip` in the same folder (creates `.Report` and `.SemanticModel` folders
   with the report and model as text, which diffs well in git). Current versions offer .pbip
   without a preview switch; older ones need **Options → Preview features → Power BI Project
   (.pbip) save option** and a restart. Whichever you saved last is the file Power BI keeps
   editing, so save the other one again after any change.
3. **File → Export → Export to PDF**. This version doesn't ask where to save: it writes the PDF to
   a temp folder and opens it in your PDF viewer; save or copy it from there to
   `powerbi/mumbai_air_quality.pdf`.
4. Screenshots: collapse the Filters / Visualizations / Data panes (the `>>` arrows) and the ribbon
   (`^` at its right end) so the page is as large as possible, then on each page press
   **Win + Shift + S** and save as `docs/img/powerbi_overview.png`, `powerbi_neighbourhoods.png`,
   `powerbi_pollutants.png`, `powerbi_seasonal.png`, `powerbi_forecast.png`.
5. Commit them. The `.gitignore` already skips the .pbip local cache files
   (`.pbi/localSettings.json`, `.pbi/cache.abf`); delete the *DAXQueries* / *TMDLScripts* folders in
   `.SemanticModel` if you used DAX query view or TMDL view for scratch work.

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
| Map visual is greyed out / "Map and filled map visuals are disabled" | They're off by default now: **File → Options and settings → Options → Global → Security → Map and filled map visuals**, or use the scatter-chart fallback from page 1 row 9 (no sign-in, nothing sent to a map service). *Azure Map* shows "To display Azure Maps visuals, sign in" until you sign in to Power BI. |
| Load fails with "PostgreSQL: Exception while reading from stream" / "another operation in the transaction failed" | The pipeline was rebuilding the marts while Power BI read them. Let the run finish, then **Apply changes** or **Home → Refresh**. |
| "Error fetching data for this visual: the top level projections must be a valid measure or aggregation" on a table | A conditional-formatting rule is based on a plain column; base it on **Sum of** the column instead. |
| A pie/column shows a "(Blank)" category for dominant pollutant | Use the current `Days Dominant` from `measures.dax` (it skips station-days with no dominant pollutant). |
| Measure error "cannot find table" | The query wasn't renamed in step 3 (still `marts dim_date`). |
