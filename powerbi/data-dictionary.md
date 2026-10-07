# Data dictionary

Model tables, their grain and source, and what each column means. Source
queries are in `sql/`.

## Stations

One row per weather station the bot trades. Source: `enrolled_stations`.

| Column | Type | Description |
|---|---|---|
| Station ID | text | ICAO airport code, for example `KLGA`. Primary key. |
| City | text | City the market is named after. |
| Latitude, Longitude | number | Station coordinates. |
| Timezone | text | IANA timezone used to define the station's local day. |
| Market Unit | text | `C` or `F`, the unit the market's brackets are quoted in. |
| Resolution Source | text | Data source the market settles on. |
| Status | text | `LIVE` if the bot trades the station, `SKIPPED` otherwise. |

## Date

One row per day from 1 Jan 2021 to 30 Jun 2026. Built in Power Query.

| Column | Type | Description |
|---|---|---|
| Date | date | Primary key. |
| Year | whole number | Calendar year. |
| Month | text | `MMM yyyy`, sorted by Month Start. |
| Month Start | date | First day of the month. |
| Weekday | text | Day name, sorted Monday to Sunday. |

## Trades

One row per bet. Source: `ledger`, filtered to bet events.

| Column | Type | Description |
|---|---|---|
| Trade ID | whole number | Ledger row id. Primary key. |
| Mode | text | `Live` for a real order, `Paper` for a dry-run bet. |
| Placed At | date/time | When the bet was placed (UTC). |
| Target Date | date | Local day whose high temperature the market settles on. |
| Strategy | text | Strategy that placed the bet: `NO` or `TAIL`. |
| Side | text | Outcome bought: `NO` or `YES`. |
| Bracket | text | Temperature range of the market, for example `17.5–18.5°C`. Derived in Power Query. |
| Model Probability | number | Model's probability that the bracket resolves YES. |
| Market Price | number | Price of the bought side when the bet was placed. |
| Edge | number | Calibrated win probability minus the price paid and fees. |
| Stake | currency | Amount bet, in USD. |
| Fill Price | number | Average price actually paid. |
| Observed High | number | Settled high temperature, in the market's unit. |
| Outcome | text | `WIN`, `LOSS`, `CLOSED` (exited early), `CANCELLED` or `PENDING`. |
| P&L | currency | Profit or loss in USD. Blank until settled. |
| Is Settled | true/false | Outcome is WIN, LOSS or CLOSED. Derived in Power Query. |
| Is Win | true/false | WIN, or CLOSED with a profit. Derived in Power Query. |

## Forecasts

One row per station, day and weather model. Source: `forecast_archive` joined
to `actuals`.

| Column | Type | Description |
|---|---|---|
| Target Date | date | Day being forecast. |
| Model | text | Weather model, for example `ICON (Germany)`. |
| Ensemble Members | whole number | Members averaged into the forecast. |
| Forecast High (°C) | number | Day-ahead forecast of the daily high. |
| Observed High (°C) | number | Observed daily high. Rows failing rule WX-01 are excluded. |
| Error (°C) | number | Forecast minus observed. Positive means the forecast was too warm. |

## Backtest Bets

One row per simulated bet per backtest run. Source:
`backtest/results/bets/*.parquet`. Runs overlap in time, so filter to one run
before totalling.

| Column | Type | Description |
|---|---|---|
| Run | text | Backtest run, named from its file. |
| Period | text | Walk-forward chunk (`A` to `D`) or `TRAIN` / `TEST`. |
| Strategy, Side | text | As in Trades. |
| Market Date | date | Day the market settles on. |
| Bracket | text | Bracket label. |
| Model Probability | number | Model's probability for the bought side. |
| Entry Price, Fill Price | number | Quoted price and volume-weighted fill from the order book. |
| Stake | number | Amount bet, in USD. |
| Won | true/false | Whether the bet won. |
| P&L | number | Profit or loss in USD. |
| Entry Time | date/time | Simulated entry time (UTC). |
| Exit Reason | text | `close` (held to settlement) or `tp` (take-profit exit). |

## Reconciliation

One row per wallet record per reconciliation run. Source:
`wallet_reconciliation_records` joined to `wallet_reconciliation_runs`.

| Column | Type | Description |
|---|---|---|
| Run ID | whole number | Reconciliation run. |
| Run At | date/time | When the run sampled the wallet (UTC). |
| Record Type | text | `trade` or `position`. |
| Match Status | text | Raw status from the bot, for example `exact` or `orphan_wallet_trade`. |
| Result | text | `Matched`, `Exception` or `Not checked` (early runs that did not match positions). |
| Trade ID | whole number | Matching ledger row, if any. |
| Amount (USD) | currency | Trade amount or current position value. |

## Data Quality

One row per validation rule. Source: `sql/data_quality_checks.sql`.

| Column | Type | Description |
|---|---|---|
| Check ID | text | Rule id. `TRD` trades, `WX` weather, `REC` reconciliation. |
| Area | text | Area the rule belongs to. |
| Rule | text | What the rule expects to be true. |
| Rows Checked | whole number | Rows the rule was evaluated on. |
| Rows Failed | whole number | Rows that broke the rule. |
| Status | text | `Pass` when no rows failed. |
