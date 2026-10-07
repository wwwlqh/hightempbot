# HighTempBot

A trading bot for Polymarket's daily high-temperature markets, and a Power BI
report analysing its results.

Each market asks which temperature range a city's high will fall in on a given
day. The bot downloads day-ahead forecasts from 11 weather models, calibrates
them per station against observed temperatures, estimates the probability of
each range, and buys when the market price is far enough below that estimate.

The bot ran on an Oracle Cloud server from April to September 2026, first in
dry-run mode and then with real money from 22 May. It is no longer running.

## Power BI report

[`powerbi/`](powerbi/) contains a Power BI Project built on the bot's data:

- **Overview**: win rate, P&L, ROI and drawdown for live and paper trading
- **Trade Explorer**: every bet, filterable by city, mode, side and outcome
- **Forecast Accuracy**: error of each weather model's forecast by city and month
- **Backtest vs Live**: backtest runs compared with live results
- **Data Quality**: 12 validation rules and ledger-to-wallet reconciliation

The data model, Power Query steps, DAX measures and SQL are all in text form.
See [`powerbi/README.md`](powerbi/README.md) for the model and how to open it.

## Results

| | Settled bets | Win rate | Return on stake |
|---|---:|---:|---:|
| Paper trading, 9–21 May 2026 | 142 | 73% | +3% |
| Live trading, 21–23 May 2026 | 25 | 76% | +60% |
| Best backtest, out of sample | 308 | 81% | +30% |

The live figure is one outlier, not an edge. A single TAIL bet turned $1.96
into $70.50; the main NO strategy won 18 of 22 live bets and still lost $1.94,
because a win at a price of about 0.80 pays 20 cents while a loss costs the
whole stake.

The backtests did not carry over to live trading. Later research found that the
bets the bot could actually fill were disproportionately the losing ones
(adverse selection), whichever forecast model was used.
[`backtest/results/research_2026_07/README.md`](backtest/results/research_2026_07/README.md)
records that analysis.

## How the bot works

1. **Ingest**: forecasts from Open-Meteo (11 models, ensemble members kept),
   observed highs from Weather Underground, and market prices and order books
   from Polymarket.
2. **Calibrate**: fit an EMOS model per station that turns the ensemble into a
   probability distribution for the daily high, retrained monthly.
3. **Decide**: price each temperature bracket, compare with the order book, and
   keep only bets whose edge clears a calibrated threshold.
4. **Execute**: walk the order book, cancel if the fill price erodes the edge,
   and record every order in a SQLite ledger before it is sent.
5. **Settle and reconcile**: settle bets when Polymarket closes the market, and
   check the ledger against the exchange wallet on every run.

A FastAPI dashboard showed positions and P&L and let the operator pause
trading.

## Repository layout

| Path | Contents |
|---|---|
| `src/hightempbot/` | The bot: ingestion, calibration, decision, execution, resolution, scheduler, dashboard |
| `tests/` | pytest suite (944 tests) |
| `backtest/` | Backtest harness, walk-forward evaluation and research results |
| `powerbi/` | Power BI report, SQL extracts and data dictionary |
| `docs/` | Runbooks and design material |

## Tech stack

Python 3.11, SQLite, pandas, SciPy, APScheduler, FastAPI, React, Polymarket
CLOB API, Open-Meteo API, Power BI (Power Query, DAX, TMDL/PBIR).

## Running locally

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

Copy `.env.example` to `.env` before running the bot with
`python -m hightempbot.main`. It starts in dry-run mode unless `DRY_RUN=False`
is set.
