# HighTempBot — Agent instructions

See `README.md` for what the bot does and `CLAUDE.md` for deployment notes.

## Code map

```
src/hightempbot/
  main.py              startup order (dry-run downgrades, reconcile, scheduler, dashboard)
  runtime_config.py    pydantic settings from .env
  stations.py          station registry
  ingestion/           Open-Meteo ensemble, WU actuals/forecast, Polymarket prices
  calibration/         EMOS fit, walk-forward LUT, reliability curves, monthly retrain
  decision/            bracket probabilities and strategy gates
  execution/
    strategy_constants.py  all trading constants and STRATEGY_CONFIGS
    pipeline.py            run_betting_cycle (safety checks, PENDING insert)
    walker.py              walk the book, place, verify, retry
    capital.py             capital / drawdown snapshot
    tp_sl_monitor.py       take-profit / stop-loss exits
    operator_control.py    Stop/Start/Transfer Lock state
  persistence/         ledger, order reconciliation, wallet reconciliation
  resolution/          settle bets from Polymarket close state
  scheduler/           per-station betting tick, market data, periodic jobs
  enrollment/          onboard new stations from Gamma events
  dashboard/           FastAPI + React SPA (static/v2/)
  db/                  connection, schema, migrations
```

## Trading invariants

- **Ensemble lock**: scoring and training require the exact `EXPECTED_MODELS`
  set. A partial ensemble corrupts EMOS — bail instead.
- **Forecast endpoint**: use `temperature_2m_previous_day1`, never
  `temperature_2m_max` (it mixes in same-day runs).
- **Strategies**: `STRATEGY_CONFIGS` is the only router input. Only NO or FLIP
  fires; `FLIP_MODE=1` swaps NO for FLIP at boot. TAIL/YMID/YHIGH are disabled.
  FLIP's `min_edge=-1.0` is intentional; its only leash is `max_walk_price`.
- **NO gate**: additive edge in [0.05, 0.15], fill price 0.75–1.00. The claimed
  probability is reliability-calibrated before edge math; without fitted
  curves the layer is identity and the gate over-fires.
- **Sizing**: `capital × capital_frac` per sleeve (no Kelly, no per-bet cap).
  The walker trims size to keep VWAP edge ≥ `execution_min_edge`.
- **Drawdown**: at `MAX_DD=0.40` no new bets until recovery. Dry-run bypasses.
- **PENDING-first ledger**: insert the PENDING row before `execute_or_log`,
  which always finalizes via `update_pending_bet_after_execution`.
- **Order sizing** uses the limit `order_price`, not `signal.fill_price`.
- **PnL is net of fees** (`pnl = gross − poly_entry_fee`). Expired PENDING
  rows get `pnl = 0.0`.
- **Schema sync**: new bet-dict fields must be added to `COLUMNS` in
  `persistence/ledger.py`.
- **Units**: empty `bracket_unit` means unknown — fail closed, never assume °C.
- **WU consensus gate** is hardcoded OFF and not env-overridable.
- **Target-date budget**: `MAX_DAILY_NOTIONAL_FRAC` is per market
  `target_date`. Never fall forward to N+2.
- **Operator controls**: Stop Processing is DB state, not a process kill.
  Start cannot override `DRY_RUN=True`. Transfers require fresh readiness,
  no PENDING/open orders, Transfer Lock and exact confirmation text.
- **Live capital** follows the `POLY_FUNDER` wallet snapshot first; the
  ledger only enriches it.

## Backtests

Read `backtest/README.md` first. Extend `evaluate_config` in
`backtest/lib/sweep_lib.py` rather than writing parallel sweep loops.
Leakage rules: EMOS as-of `calibration_params_history`, LUT from
`pred_bucket_history` before the market date, entry ≥ 4h before close.

## Workflow

- Run `pytest` before calling anything done.
- Tests of ledger/decision paths use a real SQLite connection, not mocks.
- Local and server `.env` are separate; say when a change needs both.

## Gotchas

- WU is the only actuals source. WU URLs need `ICAO:9:COUNTRY`.
- py-clob-client returns bids ascending: best bid is the last element.
- CLOB V2 collateral is pUSD; V1 orders are rejected.
- Reject NaN inputs in the walker (`NaN < x` is always False).
- Exclude CANCELLED and dry-run rows from counts and the exposure cap.
- Retrain per station on resolutions, not on a UTC calendar.
