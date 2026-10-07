---
title: Code Map
type: meta
created: 2026-05-26
updated: 2026-06-06
tags: [map, pointer-index]
status: developing
---

# Code Map

Pointer-only index. **The file is the spec; this page is the index.** No semantics here — read the file for behavior. If a row lies, fix the row, not the code.

> Renames are normal. If a row is stale (file not found, function not present), grep for the symbol and update one line. Do not write a page about the rename.

## Decision pipeline

| Concern | Read |
|---|---|
| bracket construction + per-bracket probabilities | `src/hightempbot/decision/brackets.py` |
| live signal flavors (`_compute_signal_flavors`) | `src/hightempbot/decision/strategies.py` |
| strategy gate cascade (NO / YMID / TAIL / YHIGH / FLIP) | `src/hightempbot/decision/strategies.py` |
| edge band + fill-price gates | `src/hightempbot/decision/strategies.py` |
| realized-edge depth gate + per-sleeve VWAP-slip leash plumbing | `src/hightempbot/decision/strategies.py` |
| size_frac → target USD (no static cap; idempotent top-up) | `src/hightempbot/decision/strategies.py` + `execution/strategy_constants.py::STRATEGY_CONFIGS` |
| edge-preserving book walk (Kelly retired 2026-04-24) | `src/hightempbot/execution/walker.py::walk_book_edge_preserving` |

## Calibration

| Concern | Read |
|---|---|
| EMOS fit and score | `src/hightempbot/calibration/emos.py` |
| LUT walk-forward lookup with cumulative | `src/hightempbot/calibration/lut.py::lookup_with_cumulative` |
| LUT freshness / stale halt | `src/hightempbot/calibration/lut.py` |
| LUT seed and append | `src/hightempbot/calibration/lut.py::append_triples_for_date` |
| Calibration store (LUT + EMOS coef in DB) | `src/hightempbot/calibration/store.py` |
| Monthly retrain trigger | `src/hightempbot/calibration/monthly_retrain.py` |
| Forecast model wrapper (`CalibrationModel`) | `src/hightempbot/calibration/model.py` |

## Execution

| Concern | Read |
|---|---|
| order walker (was `order.py` pre-U16) | `src/hightempbot/execution/walker.py::walk_book_edge_preserving` |
| per-strategy execution constants + `STRATEGY_CONFIGS` | `src/hightempbot/execution/strategy_constants.py` (was `config.py` pre-U16) |
| FAK first-fill + 10-min top-up + order-time slip enforcement | `src/hightempbot/execution/walker.py` |
| betting cycle orchestrator (gates → snapshot → execute) | `src/hightempbot/execution/pipeline.py` |
| TP-SL monitor (YMID/TAIL exits) | `src/hightempbot/execution/tp_sl_monitor.py` |
| capital snapshot (live-only drawdown/exposure) | `src/hightempbot/execution/capital.py` |

## Forecast and weather

| Concern | Read |
|---|---|
| Open-Meteo ensemble loader (EXPECTED_MODELS) + member fetch/cache | `src/hightempbot/ingestion/openmeteo_forecast.py` |
| WU actuals ingestion | `src/hightempbot/ingestion/actuals.py` + `ingestion/sources/wu.py` |
| WU forecast (consensus gate input) | `src/hightempbot/ingestion/wu_forecast.py` |
| Station enrollment | `src/hightempbot/enrollment/pipeline.py` |
| Geocoding + bracket/unit parser | `src/hightempbot/enrollment/geocode.py`, `enrollment/parser.py` |

## Polymarket

| Concern | Read |
|---|---|
| Polymarket price + history ingestion | `src/hightempbot/ingestion/polymarket_prices.py` |
| Bracket-bound parser (`parse_bracket_bounds`, ROUND rule) | `src/hightempbot/resolution/gamma.py` |
| Order placement (FAK BUY / FOK SELL) | `src/hightempbot/execution/walker.py::OrderClient` |
| Order reconciliation (CLOB) | `src/hightempbot/persistence/reconciliation.py` |
| Wallet reconciliation (Data API + onchain pUSD) | `src/hightempbot/persistence/wallet_reconciliation.py` |
| Auto-redeemer (Data API redeemables) | `src/hightempbot/execution/polymarket_redeemer.py` |
| Resolution settler (polymarket_* paths + Gamma close) | `src/hightempbot/resolution/settler.py` |

## Storage

| Concern | Read |
|---|---|
| ledger schema + writes | `src/hightempbot/persistence/ledger.py` (COLUMNS list must match bet dict — see [[invariants#schema-and-ledger]]) |
| DB init + migrations | `src/hightempbot/db/connection.py` |
| schema check script | `scripts/check_ledger_order_id_duplicates.py` |

## Dashboard and ops

| Concern | Read |
|---|---|
| FastAPI dashboard (cookie auth, v2 payload) | `src/hightempbot/dashboard/app.py` |
| v2 React data shaper | `src/hightempbot/dashboard/v2_data.py` |
| Operator controls (Stop/Start/Transfer Lock) | `src/hightempbot/execution/operator_control.py` |
| Telegram alerts | `src/hightempbot/execution/notify.py` |
| Scheduler jobs (APScheduler) | `src/hightempbot/scheduler/jobs.py` |
| Per-tick betting dispatch | `src/hightempbot/scheduler/betting_tick.py` |
| Capital snapshot | `src/hightempbot/execution/capital.py` |
| Startup sequence + main | `src/hightempbot/main.py` |
| Operator CLIs (`python -m hightempbot.cli.<name>`) | `src/hightempbot/cli/` |

## Backtest

| Concern | Read |
|---|---|
| Backtest harness entry (candidate gen + L2 walk) | `backtest/lib/live_match_eval.py` |
| Active champion config | `backtest/configs/candidate_l2_depth.json` (variant `sel_taila40_fp03_cs40`) |
| Sweep library (paths, flavors, leakage) | `backtest/lib/sweep_lib.py` |
| L2 decision-table build | `backtest/scripts/build_l2_decision_table.py` |
| L2 feature sweep + ABCD selector | `backtest/scripts/sweep_l2_depth_features.py`, `walkforward_l2_depth_features.py` |
| Strategy results | `backtest/results/l2_depth_*` |

## Project-level

> Note (2026-09-11): the bot is undeployed — the Oracle server was repurposed for another project. The deploy and server rows below are historical.

| Concern | Read |
|---|---|
| Deploy invariants (server, restart, schema check) — historical | `CLAUDE.md` |
| Agent workflow + trading invariants | `AGENTS.md` |
| Server SSH details — historical | [[Oracle Cloud Server]] |
