---
title: HighTempBot Project
type: source
source_type: project_repository
source_path: C:\Users\leowq\OneDrive\Desktop\hightempbot
ingested: 2026-05-03
created: 2026-05-03
updated: 2026-05-22
tags: [source, project, trading-bot, polymarket, weather]
status: developing
---

# HighTempBot Project

Source ingest of the HighTempBot codebase — an automated [[Polymarket]] trading bot that bets on daily-high-temperature markets using calibrated weather forecasts.

## What it is

A Python 3.11 service that:

1. Pulls multi-model ensemble temperature forecasts from [[Open-Meteo]].
2. Calibrates them per station with [[EMOS Calibration]] producing a Gaussian over `tmax`.
3. Looks up the empirical hit-rate of the model in a [[Walk-forward LUT]] to get `prob_safe_floor` (observed bucket rate).
4. Gates each candidate bet through a stack of decision filters.
5. Executes YES or NO bets on Polymarket's CLOB v2 using Walk-the-book Execution.
6. Logs to a PENDING Ledger Pattern, reconciles fills, and resolves same-day after a configured local hour.
7. Surfaces everything on a FastAPI HTMX Dashboard at port 8080.

## Architecture

```
src/hightempbot/
  ingestion/    Open-Meteo forecasts, Polymarket prices, station actuals
  calibration/  EMOS fit, walk-forward LUT, monthly retraining
  decision/     Strategy gates (strategies.py) + bracket math (brackets.py)
  execution/    Capital, walker (order placement), pipeline, TP/SL, notify,
                strategy_constants (formerly execution/config.py)
  persistence/  Ledger + reconciliation
  resolution/   Gamma helpers + settler tick
  scheduler/    Per-station betting tick, market data fetch, station healing,
                shared station_scanner utils, periodic jobs
  dashboard/    FastAPI + React v2 dashboard, legacy HTMX redirects
  enrollment/   Station onboarding (parser + pipeline)
  db/           SQLite connection + schema migrations
  runtime_config.py  Top-level Pydantic settings (formerly config.py)
```

(Updated 2026-05-19 to reflect the 7-phase reorg: new `decision/`, `persistence/`, `resolution/` packages; scheduler split; `execution/monitor.py` deleted; `execution/order.py` → `execution/walker.py`; `execution/config.py` → `execution/strategy_constants.py`; top-level `config.py` → `runtime_config.py`.)

Production data: SQLite at `data/hightempbot.db`; logs at `logs/`.

> [!warning] Undeployed since 2026-09-11
> The bot is not deployed anywhere as of 2026-09-11 — the [[Oracle Cloud Server]] was repurposed for another project. The live-trading status and Deployment sections below are historical.

> [!note] Live trading status (2026-05-22)
> Production is `DRY_RUN=False` after the Polymarket deposit-wallet migration. The bot trades from deposit wallet `0xFf4eCB28218af1874da6086d30D1f3125fa1c843` via `POLY_SIGNATURE_TYPE=3`; the website/proxy wallet `0xfB636718271f63E168Db16804B4E23ceA7d05FA3` may show only residual proxy-wallet funds. Latest verified dashboard smoke after the realized-capital cleanup: `capital=$108.07`, `totalPnl=$8.07`, `realizedPnl=$8.07`, `capitalSource=ledger_realized`, `resolvedCount=5`, `feesPaid=$0.28`, `walletBalance≈$48.01`. Data API open-position marks remain in wallet/operator detail and execution sizing, but do not move headline Net P&L/Capital. See Order Execution, FastAPI HTMX Dashboard, and Capital Snapshot.

## Deployment

*Historical — the bot has been undeployed since 2026-09-11.*

- Server: `opc@<server-ip>` ([[Oracle Cloud Server]])
- Bot entry: `python3.11 -m hightempbot.main` in `~/hightempbot/`
- Dashboard: http://<server-ip>:8080/
- Restart via `bash ~/hightempbot/restart_bot.sh` (kill + start as separate SSH calls — never compound)
- Local `.env` and server `.env` are independent

## Trading invariants

These are pulled directly from `AGENTS.md` and are non-negotiable:

- Ensemble Lock — exact `EXPECTED_MODELS` set required for scoring/training; partial ensemble bails the tick.
- YES+NO Trading — bot may buy either side; no `MAX_FILL` cap (LUT owns accuracy filtering); `walk_book_edge_preserving` is the single walker.
- PENDING Ledger Pattern — `pipeline.py` inserts PENDING row before `execute_or_log`; finalize via `update_pending_bet_after_execution`.
- **Sizing from limit, not signal** — `OrderClient.place_order` sizes from `order_price`, not the cached `signal.fill_price`.
- **Drawdown scope agreement** — dashboard peak-capital uses the same realized-ledger scope as the displayed headline capital card. Open positions, Data API marks, and stale wallet peaks must not create fake visible DD. Execution sizing has a separate wallet/API-aware Capital Snapshot.
- Target-date Budget — `MAX_DAILY_NOTIONAL_FRAC` is per market `target_date`, not per UTC submit date.
- **Expired PENDING `pnl = 0.0`** — expiry is bookkeeping, not a realized loss.
- **Schema sync** — new fields on the bet dict must be added to the inline INSERT column list in `persistence/ledger.py`.
- **Resolution timing** — same-day resolution requires `local_now.hour >= RESOLUTION_SCAN_START_HOUR`.
- **Calibration scope** — `_brackets_for_station` supports open-ended brackets (low/high may be `None`).

## Trading model summary

- **Forecast ensemble:** fixed `EXPECTED_MODELS` from Open-Meteo. Partial ensembles abort.
- **Bet target:** at 00Z on N+1, scanner waits for the latest complete 9-model UTC N ensemble, then trades the N+1 market date. Stations whose local date already passed N+1 skip until the next UTC cycle. Notional cap tracked per market `target_date`.
- **Calibration:** per-station EMOS → Gaussian over tmax; walk-forward LUT buckets historical (forecast, observed) pairs to estimate empirical hit rates per bracket and horizon, including open-ended top/bottom brackets.
- **Edge / sizing:** `prob_safe_floor` (LUT observed rate) drives entry edge. Per-strategy `cfg.capital_frac` × `stake_basis_capital` (NO 7%, TAIL 5%; YMID/YHIGH disabled) with cross-tick top-up for thin books; `stake_basis_capital` can use fresh wallet/Data API open value, while `deployable_capital` remains wallet-cash affordability. **Pipeline halts at 40% execution-capital drawdown**. See [[Edge-Preserving Sizing]], Capital Snapshot, and Strategy Config Registry.
- **Execution:** walk asks at submit time; abort if VWAP slippage breaks the edge gate; size from executable limit; one retry with re-walk. TAIL keeps its 3c signal band but waits for `yes_ask <= 0.02` before sizing/execution; this is the 2026-06-09 `wait_le_02c` WFO result (`+$2,537.24` B-D continuous OOS PnL).
- **Reconciliation:** background job pulls trades for open orders and writes the actual filled notional back to `kelly_size`/`bet_size`.
- **Circuit breaker:** removed — `execution/monitor.py` deleted (confirmed in 2026-05-19 reorg); its leftover `get_recent_bets` in `persistence/ledger.py` was removed 2026-08-09.

## Burned-by list (do not redo)

- Mocking the DB in decision/ledger tests — use a real sqlite connection.
- Falling back to `MAX_BET_USD` when `volume_usd` is `None` — skip the bet.
- Retraining on UTC calendar — each station has its own timezone; retrain on resolutions.
- WU URL needs `ICAO:9:COUNTRY` — verify country prefix when adding stations.
- WU concurrency: 10 workers × 0.5s is the proven safe envelope.
- Polymarket bracket format: 11 brackets per market, 2°F (US) or 1°C (non-US), shifts daily.
- WU is the only accepted actuals source — never fall back to `polyhightemp` or any non-WU stream.
- Cookie auth, not Basic — HTMX/XHR don't send Basic headers.
- Dashboard CANCELLED bets must be excluded from all counts.
- Dashboard trade counts are grouped slots; fills are ledger rows. Volume/exposure dollars remain actual filled dollars, not target dollars.
- Dry-run rows must be excluded from the exposure cap.
- Polymarket live order placement must use `POLY_SIGNATURE_TYPE=3` + deposit wallet for this account. Proxy balance visibility does not imply proxy-order compatibility.

## Pages spawned by this source

Entities: [[Polymarket]], [[Open-Meteo]], [[Weather Underground]], [[Oracle Cloud Server]]
Concepts: [[EMOS Calibration]], [[Walk-forward LUT]], [[Edge-Preserving Sizing]], PENDING Ledger Pattern, Ensemble Lock, YES+NO Trading, Walk-the-book Execution, Target-date Budget, FastAPI HTMX Dashboard
Domain: [[Polymarket Trading]]

## Active work (recent)

Repo runs single-branch on `main` (no git remote — laptop-only deployment). Feature branches that used to exist (`feat/lcb-live-deployment`, `feat/edge-preserving-sizing-and-gates`, `optimize/optimize-strategy-no`, `refactor/backtest-reorg`) have all been merged + deleted.

- **7-phase repo reorganization** (2026-05-19): U1-U16 landed as branch `refactor/backtest-reorg`, then FF-merged to `main` and the branch deleted. Created `decision/`, `persistence/`, `resolution/` packages; split `scheduler/station_scanner.py` into `betting_tick`/`market_data`/`station_healing`; removed `execution/monitor.py`; renamed `config.py` → `runtime_config.py`, `execution/config.py` → `execution/strategy_constants.py`, `execution/order.py` → `execution/walker.py`. Plan was `docs/plans/2026-05-18-001-refactor-repo-reorganization-plan.md` (deleted 2026-10-07; in git history). See [[log#[2026-05-19] reorg]].
- **Live Polymarket deposit-wallet migration** (2026-05-21): deployed deterministic deposit wallet, moved `$100` pUSD from proxy to deposit wallet, approved trading contracts, upgraded deps to `py-clob-client-v2>=1.0.1` and `py-builder-relayer-client>=0.0.2rc1`, added `hightempbot.cli.poly_wallet_diagnostics`, flipped server to `DRY_RUN=False`. See [[log#[2026-05-21] change | HighTempBot live Polymarket deposit-wallet migration]].
- **TAIL consensus-skip gate** (2026-05-17): `consensus_skip_threshold=0.50` on TAIL strategy — silently skips when any bracket in `(station, target_date)` shows YES `best_ask ≥ 0.50`. See Strategy Config Registry.
- **Cross-tick top-up** (2026-05-11, deployed 2026-05-18): replaces single-shot per-slot betting with exposure-summing idempotency + sticky walker anchor across ticks. See Cross-Tick Top-Up.
- **Candidate #1 strategy** (deployed 2026-05-09, live since 2026-05-21): 2-strategy NO+TAIL spec via multi-split optimizer. See [[Optimum Strategy]].
- **Dashboard capital/PnL correction** (2026-05-22): grouped fill rows into trade slots, floored live capital at `initial_bankroll + realized_pnl`, and fixed the startup schema guard for `operator_control_state.version`. See [[log#[2026-05-22] change | HighTempBot dashboard capital/PnL fix + schema startup guard]].

Strategy exploration runs through [[Backtest and Validation Suite]]: walk-forward [[Decision Table]], 9 [[Signal Flavors]], realistic Live-Match Evaluator.

## Sibling subdirectories

- `src/hightempbot/` — runtime application package. **Code-level deep dive: see [[HighTempBot src module]]** (file refs across all ~40 modules; ingested 2026-05-09, path-refreshed for 2026-05-19 reorg).
- `backtest/` — leakage-safe walk-forward strategy explorer. See [[Backtest and Validation Suite]] and [[Backtest Harness]].
- `validation/` — calibration validator + data backfill. See [[Walk-Forward Calibration Backtest]] and Backfill Pipeline.
