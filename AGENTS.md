# HighTempBot — Agent Instructions

This file is the single source of truth for agents (Claude Code, Codex, etc.)
working in this repo. See `README.md` for product/architecture overview.

## Knowledge wiki (read this before exploring source files)

The agent-orientation knowledge base lives **in-repo** at `wiki/` (the old
external `polybot/wiki` no longer exists).

**Always read the wiki before reading source files.** It saves tokens and
preserves context. Reading order (per `wiki/README.md`):

1. `wiki/README.md` — orientation, layout, hard rules
2. `wiki/invariants.md` — rules that look optional but aren't
3. `wiki/glossary.md` — domain terms (`p_E`, `p_L_loose`, TAIL, fp_min, …)
4. `wiki/map.md` — pointer-only index into `src/`
5. `wiki/concepts/Optimum Strategy.md` — the active production spec
   (timestamped; the only page authoritative on current strategy parameters)

After those, drill into `wiki/concepts/`, `wiki/decisions/` (why-arcs),
`wiki/postmortems/`, `wiki/entities/`, and `wiki/sources/` (module
summaries) as the task demands. The codebase is ground truth: if the wiki
and `src/` disagree, `src/` wins — fix the wiki, not the code.

## Deployment

- Server: `opc@<server-ip>` (Oracle Cloud)
- SSH key: `./ssh-key-2026-03-20.key` (in project root, gitignored; commands below assume cwd is the project directory)
- Bot: `python3.11 -m hightempbot.main` in `~/hightempbot/`
- Dashboard: http://<server-ip>:8080/

```bash
# Deploy full src tree to ~/hightempbot/src/hightempbot/ on the server
# (memory `full_module_deploy`: partial scp turns refactor-renames into
# silent runtime crashes). See CLAUDE.md for the rsync command + the
# tar-pipe fallback when rsync isn't installed locally.

# Restart bot + embedded dashboard
ssh -i "./ssh-key-2026-03-20.key" -o StrictHostKeyChecking=no opc@<server-ip> "bash ~/hightempbot/restart_bot.sh"
```

The dashboard is started by `hightempbot.main` in the same process as the
scheduler, so `restart_bot.sh` restarts both. Never split SSH `kill` and
`start` into one compound command — run them as separate SSH calls.

## Code map

```
src/hightempbot/
  runtime_config.py                  Pydantic settings (dry_run, bankroll, keys)
  stations.py                        StationConfig + STATIONS registry
  main.py                            Startup orchestration (17-phase order matters)

  ingestion/
    openmeteo_forecast.py            Multi-model ensemble pull (previous_day1 only)
    polymarket_prices.py             Token + book fetches
    actuals.py                       Observed tmax fetch + upsert
    wu_forecast.py                   WU live forecast (WU consensus gate source)
    sources/wu.py                    WU historical actuals scraper

  calibration/
    emos.py                          EMOS fit (L-BFGS-B on CRPS loss)
    lut.py                           Walk-forward bucket LUT (prob_safe_floor source)
    model.py                         CalibrationModel interface + retrain()
    store.py                         EMOS params persistence (latest + walk-forward)
    monthly_retrain.py               Per-station retrain loop

  decision/
    strategies.py                    Gate stack (edge, volume, WU consensus, etc.)
    brackets.py                      Bracket construction + probability vectors

  execution/
    strategy_constants.py            All trading constants (gates, sizing, schedule)
    types.py                         BetSignal, OrderResult, CycleResult
    capital.py                       CapitalSnapshot (realized, deployable, peak)
    live_readiness.py                Shared env/wallet/readiness GO/NO-GO checks
    operator_control.py              Stop/Start/Transfer Lock state + gates
    polymarket_transfer.py           pUSD return-transfer preview/submit safety
    polymarket_relayer.py            Deposit-wallet relayer transfer helper
    walker.py                        Walk-the-book + place + 2-step verify + retry
    pipeline.py                      run_betting_cycle (per station tick)
    notify.py                        Telegram push alerts (rate-limited)
    tp_sl_monitor.py                 TP/SL monitor (per-strategy pending close)

  persistence/
    ledger.py                        PENDING insert + finalize + fee PnL + expiry
    reconciliation.py                Backfill fills from trades API
    wallet_reconciliation.py         POLY_FUNDER wallet-first dashboard snapshot

  resolution/
    gamma.py                         Gamma close-state resolution helpers
    settler.py                       Per-station resolution tick + helpers

  scheduler/
    station_scanner.py               Shared utilities + `_notify` re-export hub
                                     (imported by sibling scheduler modules,
                                      decision/strategies.py, jobs.py)
    betting_tick.py                  Per-station betting tick
    market_data.py                   Polymarket + ensemble fetch + cache
    station_healing.py               Auto-heal unit + LUT seed helpers
    jobs.py                          Periodic jobs (monthly retrain, enrollment, prune)

  enrollment/
    parser.py                        Parse resolution source + unit from Gamma event
    geocode.py                       City → lat/lon/timezone (Open-Meteo geocoding)
    pipeline.py                      8-step station onboarding pipeline

  dashboard/app.py                   FastAPI app serving the React (in-browser JSX) SPA in dashboard/static/v2/
  db/                                Connection + schema + migrations
```

## Trading invariants (do not break)

- **Ensemble lock**: every code path that scores or trains must require the
  exact `EXPECTED_MODELS` set, not just `REQUIRED_MEMBERS` count. A partial
  ensemble silently corrupts EMOS — bail instead.
- **Sleeve registry is the authority**: `STRATEGY_CONFIGS` in
  `execution/strategy_constants.py` defines five sleeves (NO / YMID / TAIL /
  YHIGH / FLIP); the live router consults nothing else. Only NO or FLIP can
  fire: `FLIP_MODE=1` in the server `.env` (live since 2026-08-09, operator
  order) swaps NO off and FLIP on at boot (`_apply_flip_mode`). TAIL
  (disabled 2026-07-17 — EV≤0 at honest fills), YMID, and YHIGH carry
  `enabled=False`: documented, never fire. The legacy single-gate globals
  (`NO_MIN_FILL_PRICE`, `YES_MIN_FILL_PRICE`, `YES_MIN_EDGE`, `YES_MAX_EDGE`,
  `BET_SIDES`) have been removed from `strategy_constants.py` — do not
  reintroduce or cite them as gates.
- **Calibrated NO gate**: NO gates on additive edge with `min_edge=0.05` /
  `max_edge=0.15` (fp band 0.75–1.00; ceiling-bracket relaxation `p_B_50`,
  fp ≥ 0.50, max_edge 0.35). Since 2026-07-16 the claimed probability is
  reliability-calibrated BEFORE the edge/fee math
  (`RELIABILITY_CALIBRATION_ENABLED=True`): a `logit_blend` curve per
  bracket-unit group (NO_C / NO_F) loaded from `reliability_curves`, fitted
  via `cli/fit_reliability.py`. With no active curve the layer is identity
  and the raw gate over-fires — do NOT deploy without fitted curves.
- **FLIP is a deliberate −EV experiment**: FLIP buys YES on exactly the
  brackets where the NO gate fires (operator-ordered 2026-08-09 despite the
  measured −EV verdict). Its `min_edge=-1.0` is intentional — the only fill
  leash is `max_walk_price` (scanner-time ask + 0.05). Do not "fix" the
  negative edge floor; that silently turns FLIP into a no-op sleeve.
  Rollback = `FLIP_MODE=0` + restart.
- **Edge-preserving sizing (Kelly removed 2026-04-24)**: bet target is
  `capital × cfg.capital_frac` per sleeve — currently NO 0.07 and FLIP 0.07
  (TAIL 0.05, disabled). There is no static per-bet cap and no
  `MAX_BET_CAPITAL_FRAC` (constant removed). `MAX_PENDING_EXPOSURE_PCT=1.00`
  allows concurrent open exposure up to full bankroll.
  `walk_book_edge_preserving` trims actual fill size to keep realized VWAP
  edge above the sleeve's `execution_min_edge` (NO 0.05; FLIP has none — its
  walk-price leash is the only bound). Do not reintroduce Kelly.
- **Drawdown is a full halt, not half-sizing**: at `MAX_DD=0.40` realized
  drawdown, `run_betting_cycle` places no new bets until capital recovers
  above the threshold (operator decision 2026-05-20; the legacy halve-at-DD
  path and `dd_reduced` flag were removed 2026-05-23). Existing PENDINGs
  still resolve. Dry-run bypasses the halt.
- **prob_safe_floor, not LCB**: `prob_safe_floor` is the side-aware model
  probability recorded per bet (for NO it is the reliability-calibrated
  claim). LCB/UCB Wilson bands are retired — the `lcb`/`ucb` DB columns are
  dropped. Do not add them back.
- **PENDING-first ledger**: `execution/pipeline.py` inserts the PENDING row
  *before* calling `execute_or_log`. `execute_or_log` always finalizes via
  `update_pending_bet_after_execution` (defined in `persistence/ledger.py`).
  Don't bypass either side. (`enrollment/pipeline.py` is the sibling station-
  onboarding pipeline — unrelated.)
- **Sizing from limit, not signal**: `OrderClient.place_order` sizes from
  `order_price`, not the cached `signal.fill_price`.
- **Fee-adjusted PnL**: `record_resolution` and `record_position_close` store
  net-of-fees PnL (`pnl_net = pnl_gross − poly_entry_fee`). Do not write gross
  PnL directly to `ledger.pnl`.
- **WU consensus gate is hardcoded OFF**: `WU_CONSENSUS_MODE = "OFF"` in
  `strategy_constants.py` (since 2026-05-11) — no WU fetch, no verdict — and
  it is deliberately NOT env-overridable (a stray server `.env` once ran
  BLOCK by mistake and silently killed every TAIL bet). `bracket_unit` empty
  string = unknown — fail closed, never guess °C. Never fall back to
  `night.temp` for WU forecast (it's the overnight low, ~10°C below daily
  max).
- **Drawdown scope agreement**: dashboard peak-capital must use the same
  all-book scope as the displayed P&L card (`_dashboard_peak_capital`).
  Don't reintroduce `get_capital` for drawdown.
- **Live operator controls**: Stop Processing is SQLite state, not an OS kill.
  It blocks betting ticks, betting cycles, and the TP/SL monitor while keeping
  the dashboard online. Start Processing cannot override `DRY_RUN=True`.
  Transfer requires fresh readiness + wallet snapshot, no live PENDING rows,
  no open CLOB orders/positions, active Transfer Lock, and exact confirmation.
- **Dashboard wallet source**: live capital/withdrawal readiness follows the
  configured `POLY_FUNDER` wallet snapshot first. Ledger rows enrich strategy
  and station metadata; they are not the sole live-money source of truth.
- **Target-date budget**: `MAX_DAILY_NOTIONAL_FRAC` is per market
  `target_date`, not per UTC submit date. Starting at 00Z on N+1, the betting
  scanner waits for the latest complete 9-model UTC N ensemble, then targets
  the N+1 market date. If a station's local date has already passed N+1, skip
  until the next UTC cycle; do not fall forward to N+2.
- **Expired PENDING**: `pnl = 0.0` (not `-bet_size`) — expiry is bookkeeping,
  not a realized loss.
- **Schema sync**: any new field on the bet dict MUST be added to `COLUMNS`
  in `persistence/ledger.py`. Adding a field to BetSignal MUST also be added
  there if it needs to persist.
- **Resolution timing**: same-day resolution requires
  `local_now.hour >= RESOLUTION_SCAN_START_HOUR`.
- **Calibration scope**: `_brackets_for_station` supports open-ended brackets
  (low or high may be None). Don't filter them out.
- **Forecast endpoint**: always use `temperature_2m_previous_day1` (hourly
  previous-run API), never `temperature_2m_max`. The daily max endpoint mixes
  in same-day runs issued after the temperature already occurred.

## Strategy backtest harness

For any task involving "find a better strategy", "backtest", "optimize edge
gates", or similar, **always read [backtest/README.md](backtest/README.md)
first**. The harness layout:

- `backtest/lib/sweep_lib.py` + `backtest/lib/live_match_eval.py` — reusable
  libraries (signal flavors, leakage asserts, candidate generators, walk-book
  edge-preserving execution model). Imported by every script.
- `backtest/scripts/build_decision_table.py` — builds the leakage-safe parquet
  from `backtest/data/polymarket_history.db` ⨝ `data/hightempbot_server_latest.db`
  with walk-forward EMOS+LUT. Hard asserts on every row before write.
- `backtest/scripts/measure_tp_sl.py` — full TP/SL + sizing + stake-cap +
  bankroll-grid harness. Reads `backtest/configs/candidate_l2_depth.json`
  by default; override with `HTB_TP_SL_CONFIG`.
- `backtest/scripts/{sweep_consensus_skip,sweep_consensus_skip_2d,lut_*}.py` —
  parameter sweeps for specific dimensions.
- `backtest/scripts/analyze_station_pnl_stability.py` — split-half PnL
  persistence analysis (see memory `station_pnl_noise_2026_05_18`).
- `backtest/configs/candidate_l2_depth.json` — current strategy spec; live
  `STRATEGY_CONFIGS` in `src/hightempbot/execution/strategy_constants.py` must match.

Do **not** write parallel sweep loops in Python. The harness is the
single source of truth. If the parameter space needs to grow, extend
`evaluate_config` in `lib/sweep_lib.py` and update the README's prompt section.

Leakage rules baked into Phase 1:
- EMOS as-of via `calibration_params_history` (asof_date ≤ market_date)
- LUT walk-forward by aggregating `pred_bucket_history` where local_date <
  market_date (never read `lut_bucket_stats` or `calibration_params` —
  those are current snapshots, would be leakage)
- Entry price ≥ 4h before bracket close UTC

## Workflow rules

- **Plan first** for any non-trivial change (3+ steps or architectural).
- **Subagents** for research/exploration to keep main context clean.
- **Verify before "done"**: run `pytest`. For UI changes, hit the live server
  dashboard and click through.
- **Commits on `main`** are the established pattern in this repo. Use
  imperative subject + bulleted body for cross-cutting changes.
- **Deploy only on explicit request**: by default the user deploys manually
  after review, but an agent may deploy when the user explicitly asks.
- **Local vs server `.env`**: they are separate — call out when a change
  needs both.

## Things that have burned us (don't redo)

- Mocking the DB in tests of decision/ledger paths — use a real sqlite
  connection.
- Falling back to MAX_BET_USD when `volume_usd` is None — skip the bet.
- Retrain on UTC calendar — each station has its own timezone; retrain on
  resolutions, not a global gate.
- WU URL needs `ICAO:9:COUNTRY` — verify country prefix when adding stations.
- WU concurrency: 10 workers × 0.5s is the proven safe envelope.
- Polymarket bracket format: 11 brackets per market, 2°F (US) or 1°C
  (non-US), shifts daily.
- WU is the only accepted actuals source — never fall back to `polyhightemp`
  or any non-WU stream for `true_prob` / actuals.
- Cookie auth, not Basic — HTMX/XHR don't send Basic headers.
- Dashboard CANCELLED bets must be excluded from all counts.
- Dry-run rows must be excluded from the exposure cap or they block live
  trading on day 1.
- V2 CLOB migration (2026-04-22): collateral is pUSD not USDC.e; OrderArgs
  drops nonce/fee_rate_bps/taker. V1 orders are rejected.
- py-clob-client sorts bids ascending — best bid is the LAST element, not
  first.
- NaN inputs to walk_book_edge_preserving must be rejected upfront (NaN < x
  is always False, which would silently eat an arbitrarily bad book).
- Empty bracket_unit ("") means unknown unit — fail closed, do not assume °C.
  A wrong unit causes ~30°C comparison error on US °F brackets.
