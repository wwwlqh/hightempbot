# Report walkthrough

A guided tour of the report, from using it to how it is built. Each step says
what to click and what to notice. Follow it while recording your screen
(Windows: **Win + Shift + R** starts a Snipping Tool screen recording) to make a
demo video.

> In Power BI Desktop the report is in edit mode, so buttons and page tabs need
> **Ctrl + click**. Ordinary clicks on charts and slicers work as normal.

## Part 1: Using the report (about 5 minutes)

### 1. Overview page
1. Point out the layout: page tabs in the header, filters below it, KPI tiles,
   then charts. Every page follows the same layout.
2. Read the KPI tiles left to right: settled trades, win rate, P&L, ROI and
   maximum drawdown.
3. Open the **Mode** filter and pick **Live**. Every tile and chart updates.
   Clear it again with the eraser icon on the filter.
4. Hover over the cumulative P&L line to show the tooltip for one day.
5. Click a city bar in **P&L by city**. The other visuals filter to that city
   (cross-filtering). Click the same bar again to clear it.
6. Read the **Key insight** box: the live profit came from one TAIL bet.

### 2. Trades page
1. Ctrl + click the **Trades** tab.
2. In **Results by city**, click the **+** next to a city to expand it by
   strategy (drill-down in a matrix).
3. Click a city row. The trade list on the right shows only that city's bets.
4. Click the **P&L** column header in the trade list to sort by it. Red is a
   loss, green a profit, grey not settled.
5. Use the **Outcome** filter to show only LOSS, then clear it.
6. Hover over the trade list and choose **More options (...) > Show as a table**
   to show the underlying rows, then go back.

### 3. Forecasts page
1. Read the tiles: average error (MAE), RMSE, bias, share of forecasts within
   1 °C, and the best model.
2. Pick **Istanbul** in the **City** filter. The best-model tile changes: the
   most accurate model depends on the city.
3. Click a bar in **Error by model** to filter the other charts to that model.
4. Use the **Year** filter to compare 2024 with 2026.

### 4. Backtest page
1. Compare the live tiles with the **Backtest runs** table.
2. Click a run in the table. The cumulative chart highlights that run.
3. Read the insight box: why backtests did not carry over to live trading.

### 5. Data Quality page
1. Read the tiles: 9 of 12 rules pass, and 99.5% of wallet records matched.
2. Set **Status** to **Fail**. The table shows the three failing weather rules.
3. Point out **Mismatches per run**: none in the last 154 runs.

## Part 2: How it is built (about 5 minutes)

### 6. Power Query
1. **Home > Transform data** opens Power Query.
2. Select **Trades** and walk through **Applied steps** on the right: load the
   CSV, set column types, add the **bracket** label, add **is_settled** and
   **is_win**.
3. Select **Backtest Bets**: it combines every Parquet file in a folder and
   names each run from its file name.
4. Show **ProjectFolder** under Parameters: change it to move the data folder.
5. Close Power Query without applying.

### 7. Data model
1. Open **Model view** from the left rail.
2. Show the star schema: **Stations** and **Date** filter the three fact
   tables (Trades, Forecasts, Backtest Bets). All relationships are
   one-to-many, single direction.
3. Explain why **Reconciliation** and **Data Quality** stand alone: they don't
   share a grain with the other tables.

### 8. DAX measures
1. In the **Data** pane open **Key Measures** and its display folders.
2. Select **Win Rate**: `DIVIDE ( [Wins], [Settled Trades] )`. DIVIDE avoids
   divide-by-zero errors.
3. Select **Cumulative P&L**: it removes the date filter, then sums P&L up to
   the current date, and returns blank after the last trade so the line stops.
4. Select **Max Drawdown**: it builds a running equity table with ADDCOLUMNS,
   tracks the running peak, and returns the largest fall from that peak.
5. Select **Live Win Rate**: CALCULATE changes the filter context to live
   trades only.
6. Select **P&L Colour**: the measure returns a colour, used by conditional
   formatting on the P&L bars and the trade list.

### 9. Source code
1. Open `powerbi/` in File Explorer: the report is a Power BI Project, so the
   model (`.tmdl`), report pages (`.json`) and SQL are all text files under
   version control.
2. Open `sql/data_quality_checks.sql` and show a rule that uses `LAG` and
   `LEAD` to find one-day temperature spikes.

## Questions to prepare for

- **Why a star schema?** Filters flow from small dimension tables to large fact
  tables, measures stay simple, and the model compresses well.
- **Why measures instead of calculated columns?** Measures respond to filters
  at query time; calculated columns are fixed when the data loads.
- **How did you check data quality?** Twelve SQL rules covering completeness,
  ranges, uniqueness, referential integrity, spikes and reconciliation.
  Failures are flagged; only physically impossible values are excluded.
- **What would you change with more time?** Incremental refresh against a
  database instead of file extracts, row-level security if shared, and a
  longer live sample.
