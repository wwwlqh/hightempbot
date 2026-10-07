## Short Interview Closing Answer

HighTempBot is a complete, autonomous trading system for Polymarket daily-high-temperature markets: it discovers and enrolls new city markets, backfills WU actuals and Open-Meteo's 9-model day-ahead ensemble, calibrates each station with walk-forward EMOS and an 8-bucket empirical LUT, then evaluates every Polymarket bracket through a 5-sleeve strategy router (only NO enabled by default; TAIL disabled 2026-07-17; FLIP opt-in via `FLIP_MODE`), sizes edge-preservingly against the real L2 book, places fee-aware FAK orders, verifies and reconciles fills against the CLOB, settles off Polymarket's closed-state as the single source of truth, and runs TP/SL exits and auto-redemption — all driven by per-station APScheduler crons and surfaced through a hardened operator dashboard, with every tuned number traceable to a backtest harness that enforces strict walk-forward leakage gates. The defining property is that it fails closed at every boundary: it boots in dry-run, demotes to dry-run on any readiness/reconcile/wallet failure, blocks live trading when the operator-control or capital gate is unreadable, refuses to settle or transfer without verifiable proof, treats a missing quote as an information void rather than a loss, and inserts the PENDING ledger row *before* it ever touches the exchange — so the system's instinct, under uncertainty, is always to do nothing rather than risk capital.


## One-Minute Summary

HighTempBot is an automated trader for Polymarket's daily "highest temperature in <city>" markets. For each enrolled city it builds a calibrated probabilistic forecast of the day's high temperature from a 9-model Open-Meteo ensemble (dressed via EMOS — non-homogeneous Gaussian regression), converts that into per-bracket probabilities, compares them against the live CLOB order book, and places fill-and-kill limit orders when there is a fee-adjusted edge. By default it runs one enabled strategy — `NO` (buy the cheap-miss complement at a high price on brackets the model thinks won't hit). `TAIL` (a 4-of-4 unanimous-vote longshot on deep-tail brackets) was disabled 2026-07-17, `YMID` and `YHIGH` are disabled, and `FLIP` (buy YES where the NO gate fires) replaces NO only when `FLIP_MODE=1`. Resolution is Polymarket-first: a position settles when Polymarket marks the event closed, with a manual Weather-Underground-actuals fallback for stranded rows.

The runtime is a single Python process (`python -m hightempbot.main`) holding an APScheduler `BackgroundScheduler` (a 20-thread default pool plus a 4-thread "ops" pool) and a uvicorn FastAPI dashboard in a daemon thread, all backed by one WAL-mode SQLite database. Per-station cron jobs fire on staggered 10-minute slots: a betting tick, a resolution tick, a take-profit/stop-loss monitor, and a midnight actuals scrape; global jobs handle enrollment discovery, forecast/actuals backfills, retrains, reconciliation, wallet snapshots, and auto-redemption. The whole system is deliberately **fail-closed**: when any input is missing, stale, ambiguous, or unverifiable, the bot does nothing rather than risk a bad trade, and it boots in dry-run unless a long list of live-readiness checks all pass.

## Full Bot Function From Start To End

### 1. Process boot — `hightempbot.main::main()`

`main()` runs a strict ordered boot sequence. The order matters because each step's safety downgrade has to be visible to the next.

1. **Config load.** It constructs `cfg = Config()` (a Pydantic `BaseSettings` reading `.env`) and installs it as the process-wide singleton via `runtime_config::set_config`. Note for the naive reader: `main()` does *not* call `get_config()` — it seeds the singleton so that per-tick callers reuse the boot config instead of re-parsing `.env` on every tick. `Config.dry_run` defaults to `True`; `initial_bankroll` is `100.0`. `main()` keeps a **local** `dry_run` variable, and every "downgrade to dry-run" below flips this local, never `cfg`. Watch the stale assumption here: the in-code comment at `main.py:299-301` claims this is because "Pydantic BaseSettings is frozen and cfg.dry_run cannot be mutated," but that is not true of the actual code — `Config`'s `model_config` is `SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")` with **no** `frozen=True`, and Pydantic `BaseSettings` is mutable by default (`runtime_config.py::Config`). The local-variable pattern is therefore a deliberate convention, not a Pydantic freeze — the comment that says otherwise is wrong about the mechanism even though the local-`dry_run` behavior it describes is real.
2. **`cfg.ensure_dirs()`** creates `data/`, the DB parent, and `logs/`.
3. **`setup_logging()`** installs a stdout handler plus a `RotatingFileHandler` (50 MB × 5 = ~250 MB cap), resolving the path against the current cwd so launching from a foreign cwd (systemd from `/`) doesn't crash.
4. **Thread-crash alerts.** `_install_thread_exception_alerts` swaps `threading.excepthook` to log CRITICAL + page Telegram on any uncaught background-thread crash.
5. **DB init.** `db.connection::init_db(cfg.db_path)` opens SQLite with `check_same_thread=False` and applies PRAGMAs in order: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000` (5 s). It runs a pre-seed migration, then `executescript(schema.sql)`, then the full `_migrate_db` pass. This is **fail-closed at boot**: if the partial unique index `idx_ledger_order_id` fails because duplicate non-NULL `order_id` rows exist, `init_db` re-raises a `RuntimeError` pointing at `scripts/check_ledger_order_id_duplicates.py` and refuses to boot.
6. **Fee backfill** (`backfill_fee_adjusted_pnl`) — one-time gross→net PnL migration, non-fatal.
7. **Dry/live gating.** If `cfg.poly_private_key` is empty, force `dry_run=True`. Otherwise run `build_live_readiness_report(cfg)`; if it fails and `live_readiness_required` (default True) is set, force dry-run.
8. **Startup reconciliation** (live only) — `reconcile_orders` runs in a single-worker thread pool with an **outer 180 s deadline** (`fut.result(timeout=180)`). On timeout it sets the abort event, forces dry-run, and alerts. A CLOB brownout cannot block boot.
9. **Operator boot mode + dashboard configure.** `_sync_operator_boot_mode` is the *only* caller that passes `boot_dry_run` (sticky write). Then `configure_dashboard(...)` runs **after** all the downgrades, so the UI reflects the effective (possibly forced-dry-run) mode and mints a 32-byte cookie token if a dashboard password is set.
10. **Cancel dry-run PENDING** (live only): `UPDATE ledger SET outcome='CANCELLED' WHERE event_type='dry_run' AND outcome='PENDING'` — a clean slate so the simulated book doesn't eat live capacity.
11. **Station mode sync** flips `enrolled_stations.status` between `LIVE` and `DRY_RUN` to match the runtime mode.
12. **Bind-host resolution + fail-closed safety.** `_validate_dashboard_live_safety` raises (refuses boot) *only* in LIVE mode on a non-loopback host lacking either `dashboard_pass` or `dashboard_tls_terminated`. A separate, softer later check only warns+alerts so existing prod (bound `0.0.0.0` plaintext behind a firewall) keeps running.
13. **Station registration** from `get_all_stations(conn)`.
14. **Resolved-actual backfill**, then a one-shot **bracket-bounds migration** on `market_tokens` (idempotent ±0.5 conversions).
15. **Auto-retrain** of stations that have ≥30 aligned forecast+actual dates but no calibration row (LIMIT 50).
16. **Scheduler build + start**, then the **dashboard thread**, then signal handlers and `shutdown_event.wait()`.

A storage gotcha worth knowing: `schema.sql` comments `calibration_params.params_blob` as a "pickled parameter object," but `calibration/store.py` actually serializes EMOS coefficients as UTF-8 **JSON** (`json.dumps` of `a,b,c,d,n_samples`). The schema comment is stale.

### 2. Enrollment is a PREREQUISITE to scanning, not a consequence of it

This is the single most important causal fact about the system, and it's where old documentation got it backwards. **The betting tick never discovers cities.** Per-station jobs (betting, resolution, TP/SL, midnight scrape) are created only by `scheduler/jobs.py::_schedule_station_jobs`, which is invoked once per row loaded from `enrolled_stations` where `status IN ('DRY_RUN','LIVE')`. A city that has not completed `enrollment/pipeline.py::enroll_station` has **no betting tick at all**.

Discovery runs as its own global job, `_enrollment_scan_job`, on `CronTrigger(hour="0,6,12,18", minute=30, timezone="UTC")` — i.e. fixed UTC clock times 00:30 / 06:30 / 12:30 / 18:30, *not* a rolling 6-hour interval (the docstrings say "every 6h" but the cron is anchored to those wall-clock minutes). It fetches the top-100 Gamma markets by volume, extracts city names from any question containing "highest temperature," and for each genuinely new city tries the Polymarket event slug for today+1, +2, then +3 days. On the first hit it calls `enroll_station(city, event, conn, db_path, market_date=target, runtime_dry_run=dry_run)`.

`enroll_station` is an 8-step pipeline wrapped in one big try/except that converts any uncaught exception into `status=SKIPPED` (it never raises to the caller):

1. **Parse resolution source** (`parser.py::parse_resolution_source`): require "highest temperature" in the title; Tier 1 = Weather Underground (`wunderground.com` URL → ICAO), Tier 2 = HKO → `VHHH`, Tier 3 = unknown → `SKIPPED`. Unit defaults to `C` but is force-overridden to `F` for ICAOs starting `K`/`C` or prefixed `MM`/`MP` (US/Canada/Mexico).
2. **Geocode + insert.** `INSERT OR IGNORE` a placeholder row, then `geocode_city(city)`. **Critically, lat/lon/timezone come from the Open-Meteo GEOCODING API** (`https://geocoding-api.open-meteo.com/v1/search`), *not* the forecast API and *not* read out of forecast rows. This is a common error to avoid: the forecast API is used separately in step 5.
3. **Backfill actuals** day-by-day from `REF_START_DATE = "2024-03-01"`; abort after 30 consecutive failures (or skip-forward one year up to 3× when no data exists yet).
4. **Coverage gate**: skip if `actuals/expected_days < MIN_COVERAGE_PCT = 0.95`.
5. **Backfill forecasts** via the Open-Meteo *forecast* archive API (`backfill_openmeteo`) — distinct from the geocoding API in step 2.
6. **Train EMOS** for horizons 1, 2, 3 (only h=1 is verified).
7. **Seed the LUT** (`seed_lut_from_history`). If zero triples but a market exists, the row stays in the retryable `CONFIGURING` state.
8. **Register** as `DRY_RUN` (or `LIVE`), then `register_enrolled_station`.

Back in the scan, if the result is `DRY_RUN`/`LIVE`, it immediately calls `_schedule_station_jobs(enrolled_station)` — the **hot-add hookup** that makes the new station start scanning without a process restart. Conversely, an HKO/non-WU station can enroll and gets a resolution scan but gets **no** midnight scrape and **no** betting tick, because `SUPPORTED_LIVE_SOURCES = frozenset({"wu"})`.

### 3. The scheduler cadence

The betting and resolution ticks run every `SCAN_INTERVAL_MINUTES = 10` minutes — **10, not 15**, despite docstrings in `scheduler/jobs.py` and `scheduler/betting_tick.py` that say "every 15 min." The real cadence comes solely from `SCAN_INTERVAL_MINUTES`, which produces slots `[0,10,20,30,40,50]`. Each station is staggered by `icao_tick_offset(icao) = sum(ord(c) for c in icao) % 10`, so its slots are `{offset, offset+10, …}` mod 60 — this spreads the `:00` thunder-herd across the worker pool (sustained >10 concurrent WU calls caused SSL EOF). The one job that genuinely runs every 15 minutes is the hardcoded `_system_health_check` (`minute="5,20,35,50"`). Midnight actuals fire at 00:05 **station-local** time; every other global job is UTC.

### 4. `run_betting_tick` — the gate cascade

`scheduler/betting_tick.py::run_betting_tick(station, db_path, initial_bankroll, dry_run, data_dir)` is the per-station entry point. It opens its own WAL connection, sleeps a `random.uniform(0, 30)` jitter to avoid Open-Meteo concurrency errors, and then runs a strict ordered cascade. The architectural key: **no market/CLOB fetch and no forecast-data fetch happen until all the cheap gates pass**, and the forecast ensemble is fetched *last*.

The gates, in exact order (every failure short-circuits the tick):

1. **Operator gate** — `operator_control::processing_block_reason(conn, dry_run)`. Returns `None` only when operator state is `LIVE`. Fail-closed asymmetry: if the call raises, live is blocked but dry-run proceeds.
2. **Target-date / trading-window gate.** `target_date = _target_date_for_ready_cycle(now_utc)` which **returns the current UTC wall-clock date** — it explicitly does `del conn` and does *not* read `forecast_archive`, because a backfilled or future row for one station could otherwise advance every station to the wrong market date. Then a strict two-sided equality: `if local_date != target_date: return`.
3. **Cutoff-hour gate** — currently inert because `BETTING_LOCAL_CUTOFF_HOUR = 0`.
4. **Entry-hour gate** — `active_entry_hours = ⋃ cfg.entry_hour_set` over enabled strategies. With NO (`{0..6}`) and TAIL (`{1}`) enabled, that's `{0,1,2,3,4,5,6}`. At local hours 7–23 the tick skips here, before any network call.
5. **OM readiness probe** — `_last_run_ensemble_ready(now_utc)`. This is **metadata-only**: it fetches each model's `static/meta.json` and checks the last-run init/availability timestamps, cached per UTC day with a 300 s re-probe. It does **not** fetch forecast data, and it probes only 8 of the 9 models (`gem_seamless` has no meta endpoint). A naive reader assumes "readiness = forecast loaded"; it does not.
6. **Source gate** — only `wu` stations pass (`supports_live_resolution_source`).
7. **Coverage gate** — actuals coverage since `REF_START_DATE` must be ≥ 0.95.
8. **Stale-LUT gate** — `MAX(lut_bucket_stats.refreshed_at)`; attempt on-demand seeding if missing, skip if age > `LUT_STALE_HOURS = 36` (inf on parse error — fail-closed).
9. **Actuals-staleness gate** — skip if the freshest actual is older than `ACTUALS_STALE_DAYS = 35` (999 on parse error).
10. **Market-already-resolved gate** — skip if a ledger row for this station+date is `WIN`/`LOSS` with `resolution_source='polymarket_gamma_closed'`. Mode-aware (`bet` vs `dry_run`) so dry-run and live never cross-block.

**Only now** does it fetch market data: `_fetch_market_data` (a two-stage lazy cache — `market_tokens` DB with a 6 h negative sentinel and 12 h positive TTL, else a Gamma `/events?slug` lookup), then `refresh_market_volume`, then inline **CLOB enrichment** that runs price and book calls concurrently (≤6 workers) to overwrite each bracket's `best_ask`/`best_bid` with live order-book prices. (The DB cache returns prices as `0.0` on purpose; enrichment fills them.)

**Last of all**, after a live market is confirmed, it fetches the actual forecast temperatures: `_fetch_ensemble(station, target_date)` → `openmeteo_forecast::fetch_live`. So the order of network fetches is counterintuitive: **Polymarket market + CLOB first, Open-Meteo forecast data last.**

### 5. Open-Meteo forecast data and the exact-membership lock

`fetch_live` requests the day-ahead variable `temperature_2m_previous_day1` (not `daily=temperature_2m_max`, which would leak same-day observations into calibration). It tries three endpoints in order — `previous-runs` first, then `historical-forecast`, then plain `forecast` last — across 3 attempts with exponential backoff, and enforces a **strict exact-9/9 membership** per date (`_live_membership_error`): the set of non-None models must equal `EXPECTED_MODELS` exactly — missing *or* extra both reject the whole endpoint result. On total failure it falls back to a 6 h cache that re-validates membership and evicts stale entries. `_fetch_ensemble` then flattens this into a `{model_name: tmax_celsius}` dict, caching it for 22 h (the ensemble is locked for most of the UTC day).

Back in the tick, the **ensemble membership lock** is a second, exact check: `present == set(EXPECTED_MODELS)` and count == `REQUIRED_MEMBERS = 9`, else notify and return. The early readiness probe checked metadata for 8 models; this verifies the returned *data* has the precise 9.

### 6. EMOS calibration and bracket probabilities

The tick loads the EMOS model via `calibration/store.py::load_emos` (JSON blob) into a `CalibrationModel`. `is_ready()` requires `n_samples >= MIN_PAIRS = 30`; `predict()` is fail-closed (raises `CalibrationNotReadyError` if not ready). The math (`calibration/emos.py`):

```
mu    = a + b * ensemble_mean
sigma = sqrt( max( exp(c) + exp(d) * ensemble_variance , _SIGMA_FLOOR**2 ) )   # _SIGMA_FLOOR = 0.1 °C
```

The spread is **log-space on both variance terms** — `exp(c) + exp(d)·var`, not `c + d·var`, and not `exp()` of sigma itself. Ensemble variance uses `ddof=0` (population variance, per Gneiting 2005). `emos_probability` returns `P(tmax > threshold) = 1 - Φ((threshold-mu)/sigma)`. Fitting (`fit_emos`) minimizes mean CRPS via L-BFGS-B; it returns `None` only when `n_days < 10`, keeps non-converged results, and emits a log-only degenerate-fit alarm (comparing the *fitted* sigma to the floor, never the raw `d` parameter).

`decision/brackets.py::bracket_probabilities` converts each bracket's continuous `[lo, hi)` bounds to Celsius and computes per-bracket probabilities (floor: `1 - P(>hi)`; ceiling: `P(>lo)`; interior: `max(P(>lo) - P(>hi), 0)`). It is fail-closed in ordered steps: any NaN → return `[]`; total < 0.95 with a missing corner bracket → `[]`; otherwise renormalize by the total. Note the bounds it scores are the **round-rule parsed bounds** (`resolution/gamma.py::parse_bracket_bounds`, label X ↔ actual in `[X-0.5, X+0.5)`), not the integer-grid `build_brackets` output.

### 7. Walk-forward LUT lookup

For each bracket, `p_model` is mapped to one of 8 fixed probability buckets (`calibration/lut.py::bucket_of`) and looked up via `lookup_with_cumulative(conn, station, bucket, target_date)`. This is the **walk-forward anti-leakage guarantee**: it counts only `pred_bucket_history` rows with `local_date < target_date` (strict less-than, byte-identical to the backtest's `merge_asof(allow_exact_matches=False)`). It returns `(n_cum, hits_cum)` and never returns None (cold start is `(0, 0)`). The live betting gate uses this strict cumulative slice — **not** `lut_bucket_stats.observed`, which is the full-history aggregate used only for dashboards.

### 8. Strategy evaluation — `decision/strategies.py::evaluate_station`

Each bracket is run through every enabled strategy in `STRATEGY_NAMES = ("NO","YMID","TAIL","YHIGH")`; `YMID` and `YHIGH` are skipped (`enabled=False`), so only **NO and TAIL** fire in production. First, `_compute_signal_flavors(p_model, n_cum, hits_cum)` builds 9 probability variants (raw EMOS `p_E`; LUT-observed `p_L_strict`/`p_L_loose`; blends `p_B_50/30/70`; Bayesian shrinkage `p_Shrink_n10/n50`; ramp). The `p_L_*` variants go NaN on cold start; the shrinkage variants stay finite because of the +10/+50 prior — which is exactly why a separate `cold_start = n_cum < LUT_MIN_N_FOR_SHRINKAGE (30)` gate exists.

`_evaluate_strategy` runs a per-strategy gate chain in strict order: hour gate → station consensus-skip → side/price pick → fill-price band → `MIN_FILL_PRICE` → slot read (`ledger.py::slot_state`) → hourly-first-tick gate (new slots open only on tick 0; top-ups fire every tick) → legacy-slot lock → branch dispatch → volume → delayed-entry → idempotency/top-up → sizing + book walk → post-walk dust/ceiling checks.

The two live branches:

- **NO** (`_evaluate_no_branch`): a two-sided additive edge gate on `p_E`. Fill price is the NO-token `best_bid`; the band is `fp ∈ [0.75, 1.00]`, edge `= (1-p) - price - fee ∈ [0.090, 0.15]`. On ceiling brackets a relaxed extension fires (signal `p_B_50`, `fp ≥ 0.50`, edge ≤ 0.35). `prob_safe_floor` for NO is the empirical miss rate `1-p`.
- **TAIL** (`_evaluate_tail_branch`): a **4-of-4 unanimous vote** across `(p_E, p_B_50, p_L_loose, p_Shrink_n10)`, each voter tested `v ≥ 4.0 * fill_price` (the alpha-ratio is per-voter, not against the average). Requires `n_cum ≥ 30` and that all 4 voters are finite — one NaN voter fails the unanimity. The signal value is the mean of the voters.

**Sizing is fixed-fraction, not Kelly** (Kelly was removed 2026-04-24). `_compute_target_usd = cfg.capital_frac * capital` — NO is `0.07`, TAIL is `0.050`. Top-ups subtract the existing slot exposure from the target. The bet is then filled by walking the book (next section). The drawdown halt lives upstream in `pipeline.run_betting_cycle`, not here.

After all strategies, the WU consensus block is dead in production (`WU_CONSENSUS_MODE = "OFF"`), candidates are sorted by edge, and `MAX_PER_MARKET = 999` is effectively unlimited. `rank_signals` is a **separate second pass** that enforces the per-target-date notional cap (`MAX_DAILY_NOTIONAL_FRAC = 1.00`), placing best-edge-first.

### 9. The ordered safety layer — `execution/pipeline.py::run_betting_cycle`

Before any signal is placed, `run_betting_cycle` runs its own ordered fail-closed gates: operator → coverage → capital snapshot → wallet halt (live: `wallet_available`, balance ≥ $5.00) → **drawdown halt** (live: `(peak − realized)/peak ≥ MAX_DD = 0.40`) → pending-exposure cap (`MAX_PENDING_EXPOSURE_PCT = 1.00`) → ensemble present → **exact ensemble membership** (`set == EXPECTED_MODELS`, not just count) → calibration ready.

Two correctness points: the **drawdown peak basis is the realized-ledger high-water mark** (`peak_realized_capital`), not `MAX(wallet_balance)` — that's the dashboard's read pattern, but the halt deliberately uses realized PnL so old wallet samples and unrealized open positions can't fabricate drawdown. And the placement loop wraps each bracket in `BEGIN IMMEDIATE` (with bounded retries; `SKIP:lock_contention` if unacquirable) so a fresh PENDING-exposure re-check happens under the lock — cross-station ticks run concurrently, and without it siblings would all read the same stale exposure snapshot and collectively over-deploy.

### 10. PENDING-first ledger insert and book walk

`persistence/ledger.py::record_bet` inserts the row with `outcome='PENDING'` **before** any order placement, so capital tracking and the slot-exposure SUM see the stake immediately and a crash mid-verify still leaves a record. It returns `cursor.lastrowid` (the row id callers must use — never an `ORDER BY LIMIT 1` subquery).

The fill itself is computed by `execution/walker.py::walk_book_edge_preserving`: it walks ask levels cheapest-first up to the target USD, recomputing the VWAP and realized edge (`prob_safe_floor − vwap − fee`) after each level, and **stops the moment the marginal edge falls below `min_edge`**. The execution floor is per-strategy via `execution_min_edge` (NO = 0.05, TAIL = 0.07) — distinct from the gate-level edge. Fill size is bounded by the realized-VWAP edge floor, not by volume. It is fully fail-closed on NaN/Inf inputs.

`OrderClient.place_order` then quantizes the size onto the centi-share / whole-cent grid (`_quantize_market_buy_size`), validates the POLY_1271 wallet signature shape (signature type 3) and amount precision, and posts a **Fill-And-Kill** (`OrderType.FAK`) BUY. `execute_or_log` finalizes the pre-inserted row: dry-run simulates a fill (stamped `DRY_RUN_<uuid>`, `success=False`); live runs a 3-attempt walk→submit→verify loop that is idempotency-critical — once an order reaches a matched/pending state it is only **re-verified, never re-submitted** (`place_order` is not idempotent). Terminal error kinds (`auth`, `insufficient_funds`, `market_closed`, `invalid_amounts`) break the loop immediately.

### 11. Reconciliation, TP/SL, actuals, resolution

**Reconciliation** (`persistence/reconciliation.py::reconcile_orders`) runs at boot (180 s deadline) and every `RECONCILE_INTERVAL_MINUTES = 5`. It cancels truly-open orphan orders, but a **MATCHED orphan is recovered into a sentinel PENDING row, never cancelled** (it's a real fill the bot crashed before recording). It fills in missing fills, expires unfilled rows, and sweeps stranded `order_id IS NULL` PENDING rows older than 30 minutes — all guarded so a reconciler tick can never resurrect an already-resolved row.

**TP/SL monitor** (`execution/tp_sl_monitor.py::run_tp_sl_monitor`) runs offset +5 minutes from the betting tick for any strategy with a non-None `tp`/`sl` — in production only TAIL (`tp = 0.20`). It compares the **full-size executable bid-walk VWAP** (not the top bid) against `fill_price + tp`, writes a crash-safe `close_in_flight` flag before submitting an all-or-nothing `FOK` SELL, and has full orphan-close recovery (`ORPHAN_CLOSED` terminal state) when the exchange filled but the ledger write failed.

**Actuals** (`ingestion/actuals.py` + `sources/wu.py`) are scraped at 00:05 local from Weather Underground only. WU returns the station's *native* unit (°F US / °C intl); the F→C conversion happens **only in `_fetch_wu`**, keyed on `station.unit` — the persisted `actuals.tmax_celsius` is always Celsius. WU's response is hourly observations, and the daily high is `max(temps)`.

**Resolution** (`resolution/settler.py::run_resolution_tick`) runs 24/7 but gates same-local-day dates until `RESOLUTION_SCAN_START_HOUR = 18`. The settlement ladder is **Polymarket-first**. Because `EARLY_RESOLUTION_ENABLED = False`, the only active production path is `_resolve_via_gamma_close`: it requires every bracket in the event to be `closed:True`, exactly one bracket with `yes_price ≥ 0.995`, and that winner to have parseable bounds. The per-bracket Gamma path and the WU-actual fallback are deliberately *not* called automatically — WU fallback is manual-only (CLI / dashboard), gated on `POLYMARKET_FALLBACK_DAYS = 1`. A token is only declared lost with an executable `best_ask ≤ 0.005`; a missing ask is an information void, not a loss. `record_resolution` is idempotent and race-safe (refuses non-PENDING rows, `WHERE outcome='PENDING'` guard) and subtracts the Polymarket entry-side taker fee from gross PnL.

### 12. Dashboard and retrain

The FastAPI dashboard (`dashboard/app.py`) serves a React SPA at `/v2` fed by `/api/v2/data`, with cookie auth (not Basic Auth — open if no password is set), a login rate limiter, and a two-layer degraded-payload fallback so a wallet/operator failure degrades only the operator surface, not the whole dashboard. Capital, peak, and drawdown are computed from the fixed `DASHBOARD_SESSION_START_UTC` floor regardless of the range filter, because windowing the drawdown would hide what the halt gate compares against. Retrains run nightly (rolling actuals + force-retrain below `MIN_PAIRS`) and monthly (`run_monthly_retrain`, which **aborts the entire month** if the forecast backfill fails — never retrains on stale data).

### 13. The whole pipeline in one block

```
main() boot
  └─ Config + WAL DB init (fail-closed on dup order_id) + live-readiness gating
SCHEDULER (every 10 min per station, staggered by ICAO offset)
  enrollment scan (00:30/06:30/12:30/18:30 UTC)
     discover city → enroll_station 8 steps (geocode=Open-Meteo GEOCODING API)
     → register DRY_RUN/LIVE → _schedule_station_jobs (hot-add, no restart)
  run_betting_tick
     gates: operator → target-date(UTC wall-clock) → cutoff → entry-hour
            → OM readiness probe(meta.json, NOT forecast) → source → coverage
            → stale-LUT → actuals-stale → market-resolved
     fetch Polymarket market + CLOB (FIRST)
     fetch Open-Meteo forecast ensemble (LAST) → exact 9-model lock
     EMOS predict → bracket probabilities → walk-forward LUT (local_date < target_date)
     NO + TAIL gates → fixed-fraction sizing → rank_signals (daily cap)
     run_betting_cycle safety layer (drawdown=realized peak, exposure under BEGIN IMMEDIATE)
     record_bet PENDING-first → walk_book_edge_preserving → FAK place → execute_or_log
  reconcile (5 min) · TP/SL monitor (+5 min) · midnight actuals (00:05 local)
  resolution tick (24/7, ≥18:00 local) → Polymarket gamma-closed (only active path)
```

### The unifying theme: fail-closed

Every layer refuses to act on uncertainty. Boot blocks on duplicate `order_id`s and forces dry-run when readiness, the wallet, or reconciliation are unhealthy. The unit inference, ensemble membership, calibration readiness, LUT staleness, and actuals staleness gates all return early (do nothing) rather than guess. The book walker, resolution loss rule, capital snapshot, and WU forecast gate all treat NaN / missing / unverifiable data as a hard stop. `record_bet` writes PENDING before placing so a crash leaves a recoverable trail; reconciliation recovers matched orphans instead of cancelling them; resolution leaves rows PENDING until Polymarket finalizes rather than settling early. The bot's default behavior in the face of ambiguity is always: **place no bet.**

## Function Names To Mention In Interview

Knowing the exact symbol names signals you have actually read the code. This is the cheat-sheet: every entry is a real `path::symbol` from the codebase with a terse role.

### Startup And Scheduling

- `main.py::main()` — process entry point; runs the entire ordered boot sequence (config → init_db → gating → reconcile → dashboard → scheduler).
- `runtime_config.py::Config` — Pydantic BaseSettings reading `.env`; holds every tunable + secrets.
- `runtime_config.py::get_config()` / `set_config()` — process-wide Config singleton (double-checked locking); main seeds it via `set_config(Config())`.
- `db/connection.py::get_connection()` — opens sqlite3 with WAL / synchronous=NORMAL / foreign_keys=ON / busy_timeout=5000.
- `db/connection.py::init_db()` — schema seed with fail-closed IntegrityError→RuntimeError on duplicate order_id, then `_migrate_db`.
- `db/connection.py::_migrate_db()` — version-gated DROP COLUMN (SQLite>=3.35), forensic-column adds, index creation.
- `db/connection.py::_migrate_pred_bucket_history()` — rekeys LUT history to bracket-key-unique and DELETEs lut_bucket_stats to force reseed.
- `main.py::_dashboard_bind_host()` — resolves bind host (explicit → 0.0.0.0 if pass set → 127.0.0.1).
- `main.py::_validate_dashboard_live_safety()` — fail-closed: raises in LIVE on non-loopback host lacking pass or TLS-terminated.
- `main.py::_sync_operator_boot_mode()` — sole sticky writer of boot_dry_run via `get_operator_state(boot_dry_run=...)`.
- `main.py::_stop_dry_run_pending_for_live()` / `_sync_station_runtime_mode()` — live clean-slate dry-run cancel; LIVE↔DRY_RUN status flip.
- `main.py::_auto_retrain_missing_calibration()` — trains stations with ≥30 aligned dates but no calibration (LIMIT 50).
- `scheduler/jobs.py::schedule_all_jobs()` — registers EVENT_JOB_ERROR listener, clears prior jobs, schedules all per-station + global jobs, kicks boot rolling-actuals thread.
- `scheduler/jobs.py::_schedule_station_jobs()` — adds the 4 per-station jobs (midnight 00:05 local, betting, resolution, tp/sl); fail-closed on bad timezone.
- `scheduler/jobs.py::_scan_minute_slots()` — single cadence source: `range(0,60,SCAN_INTERVAL_MINUTES)` → [0,10,20,30,40,50].
- `execution/strategy_constants.py::icao_tick_offset()` / `tick_index_for()` — per-station stagger offset and its misfire-robust inverse.
- `scheduler/jobs.py::rolling_actuals_backfill_job()` — 00:15 UTC + boot-thread self-heal of a 35-day actuals window.
- `scheduler/jobs.py::_periodic_retrain_job()` / `_monthly_housekeeping_job()` / `_enrollment_scan_job()` — daily retrain (coverage/staleness gates), monthly PENDING expiry, 6-hourly discovery.
- `scheduler/jobs.py::_periodic_reconciler_job()` / `_run_wallet_snapshot_job()` / `_run_auto_redeemer_job()` — live-only interval jobs.
- `scheduler/jobs.py::_system_health_check()` — hardcoded `minute='5,20,35,50'` (the only true 15-min job).
- `dashboard/app.py::configure()` — sets dashboard module globals and mints the 32-byte cookie token when a password is set.

### Betting Tick

- `scheduler/betting_tick.py::run_betting_tick()` — per-station tick: jitter, own WAL conn, the full ordered gate cascade, then market+CLOB, then ensemble last.
- `scheduler/station_scanner.py::_target_date_for_ready_cycle()` — target date = UTC wall-clock date (`del conn`; never from forecast_archive).
- `scheduler/station_scanner.py::_last_run_ensemble_ready()` — metadata-only Open-Meteo `meta.json` readiness probe (8/9 models; gem skipped), cached per UTC day.
- `scheduler/market_data.py::_fetch_market_data()` — two-stage cache (market_tokens DB w/ 6h neg-sentinel + 12h pos-TTL) then Gamma `/events?slug`.
- `scheduler/market_data.py::_fetch_ensemble()` — singleflight-locked `fetch_live` wrapper, 22h positive cache, flattens to `{model: tmax}`.
- `scheduler/market_data.py::refresh_market_volume()` — patches volume24hr from Gamma onto each bracket.
- `execution/operator_control.py::processing_block_reason()` — operator gate: None only when state==LIVE.
- `stations.py::supports_live_resolution_source()` — source gate; only `wu` passes.
- `persistence/actuals.py::actual_source_clause()` — `LOWER(source) IN (...)` trusted-source SQL filter (fail-closed `('0', ())` when empty).
- `scheduler/station_healing.py::_attempt_seed_missing_lut()` / `_heal_station_unit_if_wrong()` — on-demand LUT seed; auto-correct station unit from bracket label.

### Forecast And Calibration

- `ingestion/openmeteo_forecast.py::fetch_live()` — live day-ahead ensemble; `temperature_2m_previous_day1`; previous-runs→historical→forecast fallback ×3 attempts; exact-9/9 membership.
- `ingestion/openmeteo_forecast.py::fetch_historical()` — single GET to previous-runs host (no fallback), 0.3s pre-sleep.
- `ingestion/openmeteo_forecast.py::_live_membership_error()` — fail-closed: every date must have EXACTLY the 9 EXPECTED_MODELS.
- `ingestion/openmeteo_forecast.py::store_forecast_records()` — one forecast_archive row per model (centre=model, member=1-based index, horizon hard-coded 1).
- `ingestion/openmeteo_forecast.py::backfill_openmeteo()` — historical populator; only stations with actuals; 365-day chunks; skips complete chunks.
- `ingestion/wu_forecast.py::fetch_wu_forecast()` — live-only WU 5day.json scrape (units=m→°C), 15-min cache; used only by the consensus gate.
- `ingestion/wu_forecast.py::_extract_tmax_for_date()` — picks max_temp; falls back ONLY to day-daypart temp, never night.
- `calibration/emos.py::fit_emos()` — L-BFGS-B CRPS fit of [a,b,c,d]; degenerate-sigma alarm; keeps non-converged result; None only if n_days<10.
- `calibration/emos.py::predict_emos()` / `emos_probability()` — (mu,sigma) for one day; P(tmax>threshold)=1-Φ((threshold-mu)/sigma).
- `calibration/emos.py::_crps_loss()` — mean `crps_gaussian` objective.
- `calibration/emos.py::EMOSParams` — dataclass a,b,c,d (c,d log-space) + n_samples.
- `calibration/model.py::CalibrationModel.is_ready()` / `.predict()` — n_samples≥30 gate; fail-closed `CalibrationNotReadyError`.
- `calibration/model.py::retrain()` — 30-day rolling fit; single dedup-by-latest-ingest forecast SELECT; requires exactly 9 EXPECTED_MODELS/day.
- `calibration/store.py::save_emos()` / `load_emos()` — JSON (not pickle) blob persist/load; threshold_bucket=0.0 for INSERT OR REPLACE.
- `calibration/store.py::save_emos_at()` / `load_emos_at()` — walk-forward asof_date memoization in calibration_params_history.
- `calibration/lut.py::bucket_of()` — maps p∈[0,1] to one of 8 BUCKETS (right-exclusive except final).
- `calibration/lut.py::lookup_with_cumulative()` — strict `local_date < asof` walk-forward (n_cum,hits_cum,mean_pred); never None.
- `calibration/lut.py::append_triples_for_date()` — one (bucket,hit) triple per bracket per resolved day using walk-forward EMOS.
- `calibration/lut.py::seed_lut_from_history()` — full expanding walk-forward seed inside a SAVEPOINT.
- `calibration/lut.py::rebuild_lut()` / `stamp_refreshed()` — re-aggregate pred_bucket_history (COUNT/SUM/AVG, Wilson retired); touch refreshed_at so idle stations don't trip stale halt.
- `calibration/lut.py::_walk_forward_params()` / `_pairs_for_fit()` — EMOS params AS OF asof_date-1; aligned-pair loader (≥20).
- `calibration/monthly_retrain.py::run_monthly_retrain()` — scheduled on the 1st of the month at 03:00 UTC via `scheduler/jobs.py` `CronTrigger(day=1, hour=3, minute=0, timezone="UTC")` (the module docstring's "2nd of each month" is stale drift); ABORTS all stations if forecast backfill fails.
- `calibration/monthly_retrain.py::refresh_lut_after_retrain()` — rebuild if lut rows exist, else seed.

### Decision Logic

- `decision/strategies.py::evaluate_station()` — per-station evaluator: build brackets, infer unit (fail-closed), EMOS probs + LUT stats, run each bracket × enabled strategy, WU consensus, MAX_PER_MARKET trim.
- `decision/strategies.py::_evaluate_strategy()` — single strategy×bracket gate chain (hour → consensus skip → fp band → slot read → hourly-first-tick → branch → volume → delayed-entry → idempotency → sizing+walk).
- `decision/strategies.py::_compute_signal_flavors()` — the 9 probability variants (p_E, p_L_strict/loose, p_B_*, p_Shrink_*, p_Ramp).
- `decision/strategies.py::_evaluate_no_branch()` — NO additive two-sided edge gate on p_E; ceiling extension via p_B_50.
- `decision/strategies.py::_evaluate_tail_branch()` — TAIL 4-of-4 unanimous vote (each voter ≥4.0×fill_price); signal=tail_vote_avg.
- `decision/strategies.py::_evaluate_ymid_branch()` / `_evaluate_yhigh_branch()` — disabled sleeves (ratio gate; ceiling-only YES additive band).
- `decision/strategies.py::_gate_wu_consensus()` — final WU agreement check; fail-closed None; inert in prod (mode OFF).
- `decision/strategies.py::rank_signals()` — second pass: best-edge-first placement under per-target-date notional budget.
- `decision/strategies.py::_extract_polymarket_brackets()` / `_compute_target_usd()` / `_execution_max_walk_price()` — bracket classification; sizing dial (capital_frac×capital); price-leash resolver.
- `decision/brackets.py::build_brackets()` — 11-bracket ladder (1 floor + 9 interior + 1 ceiling); 2°F/even-grid US, 1°C others.
- `decision/brackets.py::bracket_probabilities()` — calibrated per-bracket probs; NaN→[]; missing-corner sum<0.95→[]; renormalize.
- `decision/brackets.py::actual_in_bracket()` — single source of truth for half-open [lo,hi) membership; both-None fail-closed False.
- `decision/brackets.py::bracket_label()` / `_to_celsius()` — display formatting; display-unit→Celsius helper.
- `resolution/gamma.py::parse_bracket_bounds()` — ROUND-rule continuous bounds (label X → [X-0.5,X+0.5)).
- `persistence/ledger.py::slot_state()` — one-query (slot_filled, anchor_price, first_fill_vwap) for top-up + sticky anchor.

### Risk And Capital

- `execution/pipeline.py::run_betting_cycle()` — per-station ordered safety layer (operator → coverage → capital → wallet halt → drawdown → exposure cap → ensemble → BEGIN IMMEDIATE re-check → PENDING insert).
- `execution/pipeline.py::_should_send_halt_alert()` — 300s per-kind dedupe of wallet halt Telegram alerts.
- `execution/capital.py::get_capital_snapshot()` — CapitalSnapshot; live wallet-derived with zero-financial fail-closed sentinel; dry-run ledger path.
- `execution/capital.py::live_pending_notional()` / `_ledger_local_pending_exposure()` — pending-exposure SUM; local-only (order_id NULL) PENDING for deployable.
- `execution/capital.py::live_capital_basis()` / `_ledger_peak()` — stake basis floored at realized ledger; drawdown peak = realized-ledger high-water (NOT max wallet_balance).
- `execution/operator_control.py::get_operator_state()` / `set_operator_state()` — single-row state read; optimistic-concurrency mutation (WHERE version=?).
- `execution/live_action_guard.py::assert_fresh_live_action_context()` — separate operator-mutation guard (fresh OK readiness + fresh wallet); NOT in the betting cycle.

### Execution

- `execution/walker.py::walk_book_edge_preserving()` — cheapest-first ask walk; stops before any level pushing realized edge below min_edge (NO 0.05/TAIL 0.07) or above the walk cap.
- `execution/walker.py::_walk_real_ask_ladder()` — the real-L2 simulator branch (the codebase's actual "simulate_walk_book_l2").
- `execution/walker.py::_min_edge_for_signal()` / `_max_walk_for_signal()` — per-strategy execution edge floor; order-time price leash (None when execution_min_edge set).
- `execution/walker.py::_best_ask()` / `_best_bid()` — lowest ask (asks[0]); highest bid (bids[-1], ascending sort).
- `execution/walker.py::ClobReader` — credential-free read-only client (fetch_order_book/best_ask/best_bid/fetch_price).
- `execution/walker.py::OrderClient.place_order()` — FAK BUY: quantize to centi-share grid, validate POLY_1271 wallet shape + amount precision, post_order(FAK).
- `execution/walker.py::OrderClient.close_position()` — all-or-nothing FOK SELL with min/max VWAP re-validation.
- `execution/walker.py::execute_or_log()` — top-level executor; legacy refusal without PENDING row; dry-run sim; live 3-attempt walk→submit→verify loop.
- `execution/walker.py::_quantize_market_buy_size()` / `_resolve_terminal_state()` / `_validate_signed_order_for_wallet()` — size grid snap; degraded/leave-pending/cancel decision; sig_type=3 shape check.
- `persistence/reconciliation.py::verify_order_matched()` — polls to (True, tx_hash) only on MATCHED/FILLED with a non-null hash.
- `persistence/reconciliation.py::reconcile_orders()` — crash-recovery: orphan recover/cancel, missing-fill fill, stranded-PENDING sweep.
- `persistence/reconciliation.py::_recover_orphan_with_fill()` / `_aggregate_trades()` — sentinel RECOVERED row via INSERT OR IGNORE; trade VWAP/size/side (None on mixed side).
- `persistence/ledger.py::record_bet()` — PENDING-first 28-column INSERT; returns lastrowid.
- `persistence/ledger.py::update_pending_bet_after_execution()` — finalize PENDING row (dry/leave-pending/success/cancel; fail-closed CANCELLED on success-without-fill).
- `persistence/ledger.py::poly_fee_per_share()` / `poly_fee_charge()` / `_bet_entry_fee()` — θ·p·(1-p) taker fee; defensive charge; recompute entry fee for a row.
- `persistence/ledger.py::expire_stuck_pending()` — CLOB-verifies order_ids; leaves MATCHED/unverifiable rows PENDING (fail-closed).

### TP/SL

- `execution/tp_sl_monitor.py::run_tp_sl_monitor()` — per-station per-strategy pass: operator gate, mode-scoped PENDING rows, stale-flag age-out, hourly-first-tick gate, evaluate on full-size VWAP, set close_in_flight before close, dry vs live dispatch, orphan-close recovery.
- `execution/tp_sl_monitor.py::_evaluate_row()` — move = bid - fill_price; TP if move≥tp-1e-9 (first), SL if move≤-sl+1e-9.
- `execution/tp_sl_monitor.py::_set_close_in_flight()` / `_clear_close_in_flight()` — crash-safety flag write/remove (json_set / json_remove).
- `execution/tp_sl_monitor.py::_parse_close_in_flight()` / `_flag_started_at()` — extract flag dict; parse started_at (None → stale path).
- `execution/tp_sl_monitor.py::_close_result_has_verified_full_fill()` / `_close_result_is_explicit_safe_response()` / `_close_result_unknown_after_submit()` — the two success proofs and the ambiguous-submit (keep-flag) detector.
- `execution/walker.py::quote_close_sell()` / `close_size_fills_target()` — full-size bid-walk quote; tolerance full-fill check.
- `persistence/ledger.py::pending_positions_by_strategy()` — outcome='PENDING' rows for one strategy, event_type hard-filtered by dry_run.
- `persistence/ledger.py::record_position_close()` — PENDING→CLOSED; pnl_net = gross - entry_fee - exit_fee (both legs); raises on missing/non-PENDING.

### Actuals And Resolution

- `ingestion/actuals.py::fetch_actual()` — per-station-day entry; requires station_override; 3 retries with exponential+jitter backoff.
- `ingestion/actuals.py::_fetch_wu()` — the ONLY F→C conversion site (keyed on station.unit=='F').
- `ingestion/actuals.py::supports_actual_scrape()` / `fetch_and_store()` / `backfill_missing_actuals()` — source gate; fetch+upsert; half-open [start,end) self-heal.
- `ingestion/actuals.py::upsert_actual()` — INSERT OR REPLACE (Celsius) then ledger backfill.
- `ingestion/sources/wu.py::fetch_wu_tmax()` — api.weather.com historical.json scrape; native-unit max of hourly temps; apiKey in params; 0.5s pre-sleep.
- `ingestion/sources/wu.py::_get_country_code()` / `_read_cached_tmax()` — ICAO prefix→ISO country (2-char before 1-char); cache validate/delete.
- `persistence/ledger.py::backfill_resolved_actual_for_station_date()` — fills ledger.actual_tmax for WIN/LOSS NULL rows via actual_source_clause.
- `stations.py::fahrenheit_to_celsius()` / `celsius_to_fahrenheit()` — unit conversions.
- `resolution/settler.py::run_resolution_tick()` — per-station entry; group by target_date; RESOLUTION_SCAN_START_HOUR=18 same-day gate; backfill labels.
- `resolution/settler.py::_resolve_station_date()` — settlement ladder driver (winner → terminal_yes → terminal_token → gamma_close; per-bracket + WU fallback intentionally disabled).
- `resolution/settler.py::_resolve_via_gamma_close()` — the only ACTIVE production fallback (all-closed + exactly-one winner@0.995 + bounds).
- `resolution/settler.py::_resolve_via_wu_actual_fallback()` — manual-only WU settler (CLI/dashboard, gated on POLYMARKET_FALLBACK_DAYS).
- `resolution/settler.py::_effective_fill_price()` / `_apply_null_fill_push()` / `_terminal_token_result()` — fill-price-with-fallback (NaN→0 via isfinite); NULL-fill→PUSH downgrade; per-token WIN/LOSS/None.
- `resolution/gamma.py::fetch_gamma_resolution_markets()` / `winning_bracket_from_gamma()` — Gamma /events fetch (8s timeout); production winning-bracket gate.
- `persistence/ledger.py::record_resolution()` — idempotent race-safe PENDING→WIN/LOSS/PUSH; pnl_net = gross - entry_fee; returns True only if written.

### Dashboard And Operator

- `dashboard/app.py::v2_data()` — GET /api/v2/data: map range→days, build payload, post-hoc filter, JSONResponse.
- `dashboard/app.py::_build_v2_payload()` / `_degraded_v2_payload()` — two-layer degraded fallback; schema-complete error envelope.
- `dashboard/app.py::_check_auth()` / `login_submit()` — cookie auth dependency (constant-time compare); rate-limited /login cookie issue.
- `dashboard/app.py::_filter_v2_payload()` / `_session_floor()` / `_enrich_ledger_positions()` — pure range/month/station trimmer; fixed session cutoff; bracket/label/actual enrichment.
- `dashboard/v2_data.py::build_htb_data()` — master payload (capital/peak/dd from session floor; KPI windowed; schemaVersion 2).
- `dashboard/v2_data.py::_group_positions_for_v2()` / `_fill_detail_for_v2()` — slot-collapse with row-exact per-fill list (never aggregated).
- `dashboard/v2_data.py::_resolved_trade_stats()` — trade-group W/L tally (CLOSED+>0=win) driving KPI counts.
- `dashboard/wallet_data.py::build_operator_wallet_payload()` — {operator,wallet,readiness,liveActionsEnabled}; fail-closed liveActionsEnabled.
- `persistence/wallet_reconciliation.py::wallet_dashboard_payload()` — actionsEnabled / transferEligible / transferBlockedReason.
- `execution/operator_control.py::public_operator_payload()` — non-mutating OperatorState read + last 10 events.
- `execution/live_readiness.py::build_live_readiness_report()` / `readiness_report_is_fresh()` — live preflight (DRY-RUN short-circuits OK); freshness gate (expiresAt preferred).
- `persistence/wallet_reconciliation.py::refresh_wallet_snapshot()` / `build_wallet_snapshot()` — external fetch + 4 ledger-recovery passes; position classification + transfer-blocking warnings.
- `execution/polymarket_redeemer.py::run_redeemable_scan()` — live-only redeem: size-match gate, settle WIN/LOSS, ≤1 on-chain submit per scan.
- `execution/polymarket_transfer.py::preview_return_transfer()` / `submit_return_transfer()` — destination-pinned safe transfer; TRANSFER_LOCK + 30s re-preview TOCTOU close.

### Backtest

- `backtest/lib/sweep_lib.py::assert_no_leakage()` — hard date-ordering gate (EMOS asof≤md, LUT local<md strict, entry ≥4h before close); raises.
- `backtest/lib/sweep_lib.py::add_signal_flavors()` — the 9 p_* flavors (+ p_Recency when w/wh present).
- `backtest/lib/sweep_lib.py::parse_bracket()` / `predict_emos()` — ROUND-rule label parse (matches live); sigma floored at 0.1°C.
- `backtest/lib/sweep_lib.py::load_walk_forward_lut()` / `lut_lookup_for_rows()` / `emos_for_rows()` — cumulative+recency LUT; strict (LUT) vs inclusive (EMOS) merge_asof.
- `backtest/lib/live_match_eval.py::candidates_under_live()` / `candidates_3strats()` — 2-strategy and 4-strategy live-gate emitters.
- `backtest/lib/live_match_eval.py::simulate_walk_book()` / `_walk_real_ask_ladder()` / `_entry_arrays()` — dual-path fill; real-L2 walk; per-hour datetime64[s] leakage-unit fix.
- `backtest/scripts/build_decision_table.py::main()` — 8-step leakage-safe Parquet builder (assert_no_leakage at step 7).
- `backtest/scripts/build_l2_decision_table.py::enrich()` / `_select_snapshot()` — leakage-safe per-hour book join (at-or-before snapshot within 90-min lag).
- `backtest/scripts/walkforward_l2_depth_features.py::run()` / `train_key()` — expanding ABCD selector (A train-only); selector ranking key.
- `backtest/scripts/sweep_l2_depth_features.py::main()` — bet-stream cache + per-Variant simulate over 4 chunks.
- `backtest/scripts/measure_tp_sl.py::simulate()` / `apply_tail_delayed_entry()` — sequential bankroll sim; TAIL delayed-entry rewrite (≤0.02).

### Enrollment

- `enrollment/pipeline.py::enroll_station()` — 8-step onboarding orchestrator; returns DRY_RUN/LIVE/SKIPPED/CONFIGURING; never raises.
- `enrollment/pipeline.py::_backfill_actuals()` / `_seed_market_tokens_from_event()` — day-by-day actuals fetch (30-fail abort, 365-day skip-forward); cache bracket metadata before LUT seed.
- `enrollment/pipeline.py::should_retry_skipped_station()` / `should_probe_skipped_station()` — SKIPPED-row retry / re-parse decisions.
- `enrollment/geocode.py::geocode_city()` — Open-Meteo GEOCODING API (lat/lon/timezone), 3-retry backoff, in-memory cache.
- `enrollment/parser.py::parse_resolution_source()` / `_parse_unit()` / `_parse_wu_icao()` — 3-tier source detection + Fahrenheit override for US/CA/MX AND Panama (`icao.startswith(("K", "C")) or icao[:2] in ("MM", "MP")`); F/C default-C; ICAO extraction.
- `scheduler/jobs.py::_enrollment_scan_job()` — discovery: scan Gamma for new temperature cities, enroll, hot-add jobs.
- `stations.py::register_enrolled_station()` — registers station into STATIONS / ICAO_TO_CITY under _registry_lock.
- `stations.py::load_enrolled_stations()` / `poly_slug_for_station_id()` — load DRY_RUN/LIVE rows; resolve Polymarket slug with DB cold-start fallback.
- `ingestion/actuals.py::supports_actual_scrape()` — source=='wu' gate controlling midnight + betting jobs.
- `calibration/lut.py::seed_lut_from_history()` / `calibration/model.py::retrain()` — step 7 LUT seed; step 6 EMOS fit.

## Interview Q&A by Subsystem

This section consolidates the talking points and the hard follow-ups an interviewer will probe. The Start-to-End walkthrough above is the narrative; this is the same material in question form, with a "Say it like this" spoken-answer line where the phrasing matters. Every answer is grounded in the code; where the code contradicts a docstring, the code wins.

**Q: What's the single most important function in the codebase?**
`execution/pipeline.py::run_betting_cycle()`. It is the ordered fail-closed safety layer that gates every live bet: operator state → coverage → capital snapshot → wallet halt → drawdown halt → pending-exposure cap → ensemble present → exact ensemble membership → calibration readiness, then a per-bracket `BEGIN IMMEDIATE` exposure/cash re-check before inserting a PENDING row. Every gate returns an empty `CycleResult` early, so a single misread state halts trading rather than placing a bad order.

### Startup, SQLite & Concurrency

**Q: Why SQLite?**
A single embedded file fits a single-process bot, needs no separate service, and gives ACID transactions — which the per-bracket `BEGIN IMMEDIATE` exposure re-check and the partial `UNIQUE INDEX idx_ledger_order_id` depend on. `get_connection()` opens with `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000` so concurrent scheduler threads get bounded waits instead of immediate `SQLITE_BUSY`. The cost is the server's SQLite being 3.34.1 (no `DROP COLUMN`), which is why dead columns linger harmlessly behind the `can_drop` version gate.

**Q: SQLite is single-writer, yet a 20-thread executor plus a 4-thread `ops` executor, per-station betting/resolution/TP-SL ticks, reconcilers, and a wallet snapshotter all write. How does this not deadlock or corrupt?**
Three layered mechanisms. (1) WAL + `busy_timeout=5000` — WAL lets readers run concurrently with one writer, and the 5s timeout converts an immediate `SQLITE_BUSY` into a bounded wait; without it `BEGIN IMMEDIATE` would silently fall through to autocommit. (2) APScheduler registers per-station jobs `max_instances=1`, so a station's betting tick is serialized against itself — the slot-exposure SUM gate needs no extra lock against the same station. (3) The subtle one: `max_instances=1` does NOT serialize *across* stations, and sibling ticks all read the same tick-start `total_pending` snapshot. So the in-tick exposure re-check in `run_betting_cycle` is wrapped in `BEGIN IMMEDIATE` (write-lock with backoff `(0.0, 0.05, 0.10, 0.20)`, ≤350ms), re-`SELECT`s the fresh PENDING SUM under the lock, and only then decides — `SKIP:lock_contention` if it can't acquire, never proceeding cap-unchecked.

**Q: How do you guarantee a bet row is referenced correctly and not double-inserted on retry?**
For the row reference, `record_bet()` returns `cursor.lastrowid` and every caller uses that integer — never an `ORDER BY ... LIMIT 1` subquery, which would race under concurrent inserts. For double-submission, the *order* is the idempotent unit: once `execute_or_log` reaches `matched_order_id`/`pending_order_id`, retries only re-verify that same order; `place_order` is never re-called, and `_find_open_matching_order` is scoped to `bot_placed_ids` so it won't adopt a foreign order. The partial `UNIQUE INDEX idx_ledger_order_id ... WHERE order_id IS NOT NULL` makes orphan-recovery `INSERT OR IGNORE`s idempotent; NULL order_ids (dry-run, pre-fill PENDING) bypass it.

**Q: What actually breaks at scale?**
The single-writer property. At high station counts, `BEGIN IMMEDIATE` write-locks plus reconcilers plus wallet snapshots raise contention and the `busy_timeout=5000` waits eat into the 10-minute scan budget. Mitigations: the per-ICAO stagger `icao_tick_offset(icao) = sum(ord(c) for c in icao) % 10` spreads stations across slots `[0,10,20,30,40,50]` (memory: >10 concurrent WU workers caused SSL EOF), and `max_instances=1` makes slow ticks *skip* rather than queue. But the real ceiling is SQLite — this is a single-box, single-file design fine for ~10–50 stations; past that you'd need Postgres or sharded DBs.

**Say it like this:** "Boot is a fail-closed ladder — config into a process singleton so per-tick callers don't re-parse `.env`, WAL DB with a 5-second busy timeout, then a sequence of live-safety gates any one of which flips a local `dry_run` flag. `configure_dashboard` runs *after* those downgrades so the UI never lies about being live, and duplicate `order_id`s hard-fail boot because that index is what keeps reconciliation from creating duplicate recovery rows."

### Scheduler & Market Date

**Q: What's the scan cadence, and how are stations staggered?**
Every `SCAN_INTERVAL_MINUTES = 10` minutes — 10, not 15; the "15 min" docstrings are stale. Slots are `[0,10,20,30,40,50]`, each station offset by `icao_tick_offset(icao) % 10` so its slots are `{offset, offset+10, …}` mod 60, spreading the `:00` thunder-herd. The gate side recovers the index with `tick_index_for`, which survives a one-interval misfire. The only true 15-min job is the hardcoded `_system_health_check` (`minute="5,20,35,50"`).

**Q: Where does the target market date come from?**
`_target_date_for_ready_cycle()` does `del conn` and returns `now_utc.date()` — the current UTC wall-clock date, NOT anything inferred from `forecast_archive` (a backfilled/future row for one station would otherwise advance every station to the wrong date). The trading window is a strict two-sided equality: `if local_date != target_date: return`.

**Say it like this:** "Scans run every 10 minutes; each station gets a deterministic ICAO offset so they don't all hammer Open-Meteo at :00. The target date is just today's UTC date — we deliberately don't derive it from forecast rows. The tick is a cheap-to-expensive gate cascade, and the model's forecast is fetched dead last, only after a live market is confirmed."

### Enrollment

**Q: How does a city become tradable?**
Enrollment is a PREREQUISITE, not a consequence of scanning — the betting tick never discovers cities. `_enrollment_scan_job` (CronTrigger `hour="0,6,12,18", minute=30` UTC — fixed clock times, not a rolling 6h interval) pulls the top-100 Gamma markets by volume, extracts "highest temperature" cities, and calls `enroll_station`. That's an 8-step pipeline wrapped in one try/except that converts any uncaught exception into `status=SKIPPED` — it never raises; callers inspect the return (`DRY_RUN`/`LIVE`/`SKIPPED`/`CONFIGURING`). Steps: parse source → geocode+insert → backfill actuals → coverage gate (≥0.95) → backfill forecasts → train EMOS h=1,2,3 → seed LUT → register. On `DRY_RUN`/`LIVE` it calls `_schedule_station_jobs` directly — the hot-add that starts scanning with no restart.

**Q: Where do lat/lon/timezone come from?**
The Open-Meteo GEOCODING API (`geocoding-api.open-meteo.com/v1/search`), NOT the forecast API and NOT forecast rows — the forecast API is used separately in step 5. This is the most-missed fact. Only `wu` stations are live-tradeable (`SUPPORTED_LIVE_SOURCES = frozenset({"wu"})`), so an HKO station can enroll and gets a resolution scan but no midnight scrape and no betting tick.

**Say it like this:** "Enrollment is an 8-step pipeline that never throws — it returns a status string, including a non-terminal `CONFIGURING` when the brackets aren't listed yet. Coordinates come from Open-Meteo's geocoding endpoint, a different API from the forecast one. It gates on 95% actuals coverage and a successful h=1 EMOS fit before flipping to DRY_RUN/LIVE, and discovery hot-adds the scheduler jobs."

### Forecast Ingestion

**Q: Why `temperature_2m_previous_day1` instead of `daily=temperature_2m_max`?**
So every forecast is genuinely day-ahead — the forecast issued by the *previous* day's model run — with no same-day observation leaking into calibration. `daily=temperature_2m_max` is explicitly forbidden. `fetch_live` tries `previous-runs` → `historical-forecast` → plain `forecast` (last, 30s) across 3 attempts, preserving train/live symmetry, and enforces strict exact-9/9 membership per date (missing *or* extra rejects the whole result). On exhaustion it falls back to a 6h cache that re-validates membership.

**Q: How many models, and why does the count matter?**
Exactly 9 (`EXPECTED_MODELS`); the "11 models" docstrings are stale (BoM offline, JMA excluded). Trust `EXPECTED_MODELS`. (See the EMOS exact-membership question below for why a count check isn't enough.)

**Say it like this:** "We use the previous-day-run variable so every forecast is day-ahead and live matches training — no same-day leakage. Nine models, not eleven. We reject any date that doesn't have exactly the 9-model set, and each model is stored as its own `forecast_archive` row keyed by `centre`."

### EMOS / Calibration

**Q: Why EMOS, and why a Gaussian?**
EMOS (non-homogeneous Gaussian regression) dresses the raw 9-model ensemble into a calibrated predictive Gaussian: `mu = a + b·ensemble_mean`, `sigma = sqrt(exp(c) + exp(d)·ensemble_variance)`, fit by minimizing mean CRPS via L-BFGS-B. Raw ensemble means are biased and overconfident; EMOS corrects both location and spread. Daily-max temperature errors are close enough to Gaussian that the fit is well-behaved, and the exceedance probability `1 - Φ((threshold-mu)/sigma)` is a clean closed form — a fatter tail would complicate the bracket integration for no demonstrated edge. The log-space spread (both terms `exp()`'d) guarantees `sigma² > 0` for any optimizer state. (Full coded formula and parameter bounds are in the Stats Fundamentals section below.)

**Q: Why apply the LUT after EMOS rather than betting on EMOS probability directly?**
EMOS gives a model probability; the LUT measures the empirical hit rate of each probability bucket from resolved history — "when EMOS said 0.30, how often did it happen?" The walk-forward `L_obs = hits_cum / n_cum` feeds the LUT-derived flavors (`p_L_strict/loose`, `p_B_50`, `p_Shrink_n10/n50`) that drive TAIL and NO's ceiling extension, so a miscalibrated bucket can't keep those signals firing. The nuance: NO's *strict* gate does NOT use the empirical rate — `prob_safe_floor = 1 - p` where `p = p_E`, the raw EMOS bracket probability; the empirical rate only reaches NO through the `p_B_50` ceiling extension. EMOS is the prior; the LUT is the realized correction layered onto everything except NO's strict path.

**Q: What happens when the optimizer doesn't converge, or the fit is degenerate?**
Non-convergence is fail-*soft*: `result.success == False` logs an error but still returns `result.x` ("partial convergence is usually usable"); `fit_emos` returns `None` only when `n_days < 10`. The degenerate-fit alarm is separate and log-only: it recomputes the *fitted* per-row sigma (NOT the raw parameter `d`, which is on a log scale) and compares to `_SIGMA_FLOOR + 1e-6` — CRITICAL if all rows collapsed to the floor, WARNING if some — but never aborts or alters params. The real fail-closed gate is downstream: `is_ready()` requires `n_samples >= MIN_PAIRS = 30`, and `predict()` raises `CalibrationNotReadyError`.

**Q: Why require *exactly* the 9 `EXPECTED_MODELS`, not just count to 9?**
A wrong-but-9-member set — a stale or substituted centre — passes a count check but silently changes the calibration the model was fit against. `run_betting_cycle` requires `set(present) == set(EXPECTED_MODELS)`; exact equality fails closed on any substitution, and the ensemble is then fed in fixed `EXPECTED_MODELS` order for deterministic column alignment. Two checks with different scopes: the early readiness probe checks `meta.json` for 8 of 9 (gem_seamless has no meta endpoint); the post-fetch lock verifies the exact 9 in the data. Passing the probe does not guarantee passing the lock.

**Say it like this:** "EMOS is a Gaussian regression: mean is affine in the ensemble mean, variance is `exp(c) + exp(d)·ensemble_variance` — both spread terms in log space so sigma is always positive. We fit a,b,c,d by minimizing CRPS and floor sigma at 0.1°C. The degenerate-fit alarm compares the *fitted* sigma to the floor, not raw `d`. The exceedance probability is just `1 - Φ((threshold-mu)/sigma)`."

### LUT / Leakage

**Q: Define leakage precisely. What's the exact predicate, and why is EMOS treated differently from LUT?**
Leakage is using information dated at or after the day being predicted. The asymmetry — the single most-gotten-wrong invariant — is in `sweep_lib.py::assert_no_leakage`: `emos_bad = (asof_date > market_date)` but `lut_bad = (local_date >= market_date) & notna()`. EMOS uses `<=` (params dated on the market date are fine, because the walk-forward params for day D are fit on the window ending D-1, so they're leak-free); LUT must be strict `<` (a same-day resolution is a future observation). In live code, `lookup_with_cumulative` uses `local_date < asof_local_date`, byte-identical to the backtest's `merge_asof(direction='backward', allow_exact_matches=False)`; EMOS uses `allow_exact_matches=True`. A NaT cold-start `local_date` is explicitly exempt.

**Q: A bucket has only 5 observations. What does the model do, and where does `MIN_BUCKET_SAMPLES` actually bind?**
A trap: `MIN_BUCKET_SAMPLES = 30` (removed from `strategy_constants.py` 2026-08-09; it was never consulted by any live gate). The binding cold-start cutoff is `cold_start = n_cum < LUT_MIN_N_FOR_SHRINKAGE (30)`. When `n_cum < 30`: `p_L_strict` becomes NaN, `p_L_loose` falls back to raw EMOS, NO/YMID/YHIGH emit a SKIP, TAIL skips via its own `vote_min_n=30` guard. The shrinkage variants stay finite even at `n_cum=0` because of the +10/+50 prior — which is exactly why the cold-start guard exists separately from the NaN guard (the NaN check alone never catches a collapsed shrinkage value).

**Q: There's `lut_bucket_stats.observed` and the cumulative `hits_cum/n_cum`. Which does the betting gate use?**
The cumulative walk-forward slice (`hits_cum/n_cum` with the strict `local_date < target_date` predicate). `lut_bucket_stats.observed = hits/n` over ALL history is dashboard/forensics only. Conflating them is the biggest place a reader goes wrong — the full-history aggregate would leak same-day and later resolutions into a live decision. (Dashboard `lut_total_n` is the count of seeded distinct station-DAYS, not `SUM(bucket.n)` — summing inflates ~3×.)

**Q: When does `assert_no_leakage` run, and what if it trips?**
At Step 7 of `build_decision_table.py::main()`, *before* the Parquet is written. It raises `AssertionError` with the first 3 offending rows on any of three masks (EMOS-asof, LUT-local, or entry-too-late: `entry_ts + hours_before_close*3600 > close_ts`, default 4h). A leaky table is never persisted.

**Say it like this:** "The LUT is a per-bucket empirical hit rate, and the anti-leakage guarantee is in the lookup: it sums only rows strictly before the market date, byte-matched to the backtest's `merge_asof` with `allow_exact_matches=False`. Wilson confidence bands are retired; `prob_safe_floor` is the raw observed rate. Triple generation is fail-closed — it won't write unless the ensemble is exactly the 9 models, deduped by latest ingestion."

### Bracket Math

**Q: An observed high lands exactly on a bracket boundary — 70.0°F for a "70-72" bracket. Win or loss?**
Membership is half-open `[lo, hi)` via `actual_in_bracket` (floor `actual < hi`; ceiling `actual >= lo`; interior `lo <= actual < hi`). And the bounds aren't the integer grid: under the ROUND rule (`parse_bracket_bounds`), label X covers continuous `[X-0.5, X+0.5)`, so "70-72" is really `[69.5, 72.5)`. The actual is rounded to display units first, then tested. Half-open is what lets the contiguous ladder partition the real line with no double-counting at any edge; a both-None bracket fails closed (returns False), never matching everything.

**Q: Why does the F ladder differ from the C ladder?**
`build_brackets` produces 11 brackets (1 floor + 9 interior + 1 ceiling). F path: `center = round(F(median))`, `low = center - 9`, parity correction subtracts 1 only when low is odd (forces even, not round-to-nearest), 9 interiors width 2, ceiling `low+18`. C path: `center = round(median)`, `low = center - 4`, 9 interiors width 1, ceiling `low+9`. The asymmetry (F width-2/offset-9/even-parity vs C width-1/offset-4/no-parity) is what trips people up. `bracket_probabilities` is fail-closed twice: any NaN → `[]`; sum < 0.95 with a missing corner → `[]` (returning `[0.0]*N` would make every NO bet look like free money); then renormalize.

### Strategy, NO & TAIL

**Q: How is each bracket evaluated?**
Every Polymarket bracket runs through the 5-sleeve stack `(NO, YMID, TAIL, YHIGH, FLIP)`; by default only NO is `enabled=True` (TAIL disabled 2026-07-17; `FLIP_MODE=1` turns NO off and FLIP on at boot). Per bracket, `_compute_signal_flavors` builds 9 probability variants: `p_E` (raw EMOS), `p_L_strict/loose` (loose falls back to raw EMOS when `n_cum < 30`), three blends `p_B_50/30/70`, two shrinkages `p_Shrink_n10/n50`, and `p_Ramp`. Only `p_L_strict` goes NaN on cold start; loose/shrinkage/ramp stay finite via the prior — hence the separate cold-start guard. `_evaluate_strategy` then runs an ordered gate chain: hour → consensus-skip → side/fill-price band → slot read → hourly-first-tick (new slots open only on tick 0; top-ups every tick) → legacy-slot lock → branch (edge gate) → volume → delayed-entry → idempotency/top-up → sizing + book walk → post-walk dust/ceiling. `rank_signals` is a separate second pass enforcing the per-target-date notional cap.

**Q: What exactly is the NO gate?**
NO buys the NO token (`fill_price = mkt['best_bid']`, the NO token's own executable price despite the legacy key name) on price band `[0.75, 1.00]` with a strict two-sided additive edge band `[0.090, 0.15]` on `p_E`, edge `= (1-p) - price - fee`, `fee = 0.05·p·(1-p)`, `prob_safe_floor = 1-p`. On a ceiling bracket that misses the strict gate, a relaxed extension fires (`p_B_50`, `fp ≥ 0.50`, edge ≤ 0.35). NO does NOT use the global `MIN_EDGE=0.03`/`MAX_EDGE=0.10`. Sizing is `capital_frac = 0.07`. Its realized-VWAP execution floor is `execution_min_edge = 0.05` (distinct from the 0.090 entry gate); because that floor is set, `_max_walk_for_signal` returns None — the only fill boundary is realized VWAP edge, not a 5-cent price leash.

**Q: What exactly is TAIL?**
A 4-of-4 unanimous-vote contrarian sniper on the YES side. Requires `n_cum >= 30`, then each voter in `(p_E, p_B_50, p_L_loose, p_Shrink_n10)` must independently satisfy `v >= 4.0·fill_price` — passing needs `votes_pass >= 4 AND votes_total >= 4`, so a single NaN voter fails the whole gate. The alpha test is per-voter, not on the average (the average is only `prob_safe_floor`/edge). Fill band `[0.001, 0.03]`, but `delayed_entry_fp_max = 0.02` means TAIL records "signal good, waiting" telemetry up to 0.03 yet can't size/place until the YES ask drops to ≤0.02 — the extra two cents is where the edge thins out. Fires only at local hour 1, `capital_frac = 0.050`, execution floor 0.07, `consensus_skip_threshold = 0.40` (skip all TAIL when any bracket in the station/date shows YES ask ≥ 0.40), `tp = 0.20`. In the backtest, delayed entry rewrites each TAIL bet to the first later snapshot at ≤0.02 (bounded by close−4h) and *drops* bets that never reach it — a real filter.

**Say it like this:** "Each bracket is scored independently by every enabled strategy off nine probability flavors. The cold-start guard is separate from the NaN guard because only `p_L_strict` actually goes NaN. NO buys the cheap-miss complement at a high price with a 9–15 point additive edge band; TAIL is a unanimous 4-of-4 vote where each flavor must clear 4× the fill price, only enters at ≤2 cents at hour 1, and carries a +0.20 take-profit."

### Execution / Walker

**Q: Why walk the order book instead of taking a single limit price?**
`walk_book_edge_preserving` consumes ask levels cheapest-first, recomputing VWAP and realized edge (`prob_safe_floor - vwap - fee`) after each level, and stops the moment the marginal new edge drops below the per-strategy floor. Fill size is bounded by realized edge, not liquidity — every executed bet is guaranteed `realized_edge >= min_edge` and `filled_usd >= MIN_BET_USD (1.0)`, or it returns None. It fails closed on any non-finite input (`NaN < x` is always False, which would otherwise eat an arbitrarily bad book). A flat limit would either underfill or eat the book past the point the edge is gone.

**Q: What are the exact edge floors, and how do they differ from the entry-gate edges?**
Execution VWAP floors are per-strategy via `_min_edge_for_signal`: NO `0.05`, TAIL `0.07` — distinct from the pre-entry gate edges (NO's gate band is `[0.090, 0.15]`, but its realized VWAP may degrade to 0.05). The globals `MIN_EDGE=0.03`/`MAX_EDGE=0.10` are imported but NOT used by the live router — only a fallback for unknown strategies. Because both NO and TAIL set `execution_min_edge` (and `max_vwap_slip_from_anchor=None`), `_max_walk_for_signal` returns None — there is no 5-cent price leash; the VWAP edge floor is the sole boundary.

**Q: Why FAK for entries but FOK for closes?**
`place_order` posts `OrderType.FAK` (Fill-And-Kill): take whatever the book offers at-or-below the limit, cancel the rest, leave nothing resting — matching the edge-preserving walk; a partial fill is fine and reconciled from actual matched trades. `close_position` posts `OrderType.FOK` (all-or-nothing) because a partial TP/SL exit must never be booked as CLOSED. The bot never uses GTC — it won't leave a resting maker order exposed to adverse selection on a 1-day market. `execute_or_log` runs a 3-attempt loop and is idempotency-critical because `place_order` is not idempotent: once an order is matched/pending it's only re-verified; terminal kinds (`auth`, `insufficient_funds`, `market_closed`, `invalid_amounts`) break immediately; a fill is booked only with a real transaction hash.

**Say it like this:** "The walker walks the ask book cheapest-first and stops the moment the marginal realized VWAP edge falls below the strategy's floor — 5 points NO, 7 TAIL — and fails closed on NaN. The retry loop is built around placement not being idempotent: once submitted, we re-verify rather than re-submit, and we only adopt orders we placed ourselves. FAK entries, FOK closes, no GTC."

### Risk / Capital

**Q: Why not Kelly sizing?**
Kelly (`f* = edge/odds`) is growth-optimal in theory but fragile to estimation error in `p`, and a weather-market edge has real model error, so full Kelly over-bets and fractional-Kelly tuning is unstable. Kelly was removed 2026-04-24; sizing is now `target_usd = capital × capital_frac` (NO 0.07, TAIL 0.050), filled edge-preservingly, with a portfolio drawdown halt and exposure cap. No static dollar cap, no Kelly multiplier; the ledger still writes the same value to both `kelly_size` and `bet_size` for backward compatibility, but the name is vestigial.

**Q: Walk through how sibling concurrent ticks can blow the exposure cap, and the fix.**
`MAX_PENDING_EXPOSURE_PCT = 1.00` caps open exposure at 100% of `stake_basis_capital`. The top-of-tick gate reads `pending_exposure` once, but `max_instances=1` only serializes a station against itself — five stations' ticks run concurrently and all read the same stale tick-start snapshot, so they'd collectively over-deploy. The fix is the per-bracket read-modify-write under `BEGIN IMMEDIATE`: re-`SELECT` the fresh PENDING SUM (seeing sibling commits), compute `projected = fresh + bet_size`, and if `> exposure_cap` roll back and `SKIP:exposure_cap_race`. A parallel deployable-cash re-check exists too; both are `not dry_run`-guarded.

**Q: What's the drawdown denominator — the wallet balance?**
No, the most-gotten-wrong risk question. `drawdown = (peak - realized)/peak >= MAX_DD (0.40)`, but `peak = peak_realized_capital = max(ledger_peak - return_transfers, stake_basis)` — the realized-ledger high-water mark, NOT `MAX(wallet_balance)`. Deliberate: old wallet samples and unrealized open positions must not fabricate drawdown. `MAX(wallet_balance)` is only the dashboard's wallet-audit read. The halt is live-only (so an operator can flip to dry-run to investigate); it suspends *new* placement while existing PENDING still resolve; the old "halve target" band was removed. A live wallet read failure returns an all-zeros sentinel with `wallet_available=False`, which the wallet halt turns into a hard stop — silent ledger fallback is forbidden (`$5.00` min balance is a hardcoded literal).

**Q: How do dry-run rows interact with live exposure?**
Exposure/idempotency queries use mode-aware `event_type` filtering: `("bet","dry_run")` in dry-run, `("bet",)` in live. A stale dry-run PENDING counts toward exposure *within* dry-run but can never block live (memory: `dryrun_exposure_block`). On live boot, `_stop_dry_run_pending_for_live` cancels all `event_type='dry_run' AND outcome='PENDING'` rows for a clean slate (settled dry_run rows are kept for historical PnL). The in-tick race re-checks are additionally `not dry_run`-guarded.

**Say it like this:** "Risk is an ordered fail-closed gate stack. The drawdown halt measures peak-to-trough on the *realized ledger* high-water mark, not the wallet, so unrealized positions or stale reads can't trigger a phantom halt. The ensemble gate is exact set equality, not a count. And in the placement loop we take `BEGIN IMMEDIATE` and re-read the live pending sum under the lock, because sibling ticks run concurrently and a stale snapshot would let them blow past the exposure cap."

### Ledger & Reconciliation

**Q: Why insert the PENDING row *before* placing the order? Isn't that lying about state?**
It's the only crash-safe ordering. `record_bet` inserts `outcome='PENDING'` and commits BEFORE any CLOB call, returning `lastrowid`, so capital tracking and the slot-exposure SUM see the stake immediately and a crash mid-verify leaves a recoverable record. Confirm-then-write would lose the bet on a crash and orphan a real on-chain fill. `update_pending_bet_after_execution` owns the transition in four branches — dry_run, leave_pending, success, and a fail-closed CANCELLED when `success=True` but `fill_price`/`fill_size` is None (a contract violation, never NULL-wiped). `outcome` stays the literal `'PENDING'` through FILLED — FILLED is not a distinct label; terminal labels are WIN/LOSS/PUSH/CLOSED/CANCELLED/EXPIRED.

**Q: Enumerate the crash windows and how each is bounded.**
Three. (1) Crash between PENDING-insert and `order_id` set → `order_id IS NULL`; the stranded-PENDING sweep cancels these when `bet_ts < now - 30 min`. (2) Crash between `place_order` and the ledger UPDATE → a CLOB orphan; a MATCHED/FILLED orphan is *recovered* into a sentinel `RECOVERED` PENDING row (never cancelled — that's a real fill), idempotent via the partial unique `order_id` index; only a non-matched, non-terminal orphan is cancelled; `_aggregate_trades` returns side=None on mixed YES+NO and refuses to book an inverted bet. (3) Order filled but fill never recorded → the missing-fills loop reads `get_trades`; if unknown (API timeout) it leaves the row PENDING and fails closed. The window is bounded by `max(boot_recon, RECONCILE_INTERVAL_MINUTES=5)`; startup reconcile runs in a worker thread with `fut.result(timeout=180)`, on timeout sets `abort_event`, forces dry-run, and alerts.

**Q: A row is stuck PENDING for hours. Does it permanently lock its slot against top-ups?**
No — a combined predicate prevents both a permanent lock and double-staking. In `slot_state`, stale PENDING rows (`bet_ts` older than `MAX_PENDING_AGE_MINUTES = 240`) are subtracted from the slot's exposure SUM ONLY when `n_filled > 0`. So a slot with a real fill plus a zombie PENDING drops the zombie and allows a top-up; but a PENDING-only slot stays fully counted so a top-up can't double-stake against an in-flight order. `RECONCILE_INTERVAL_MINUTES=5` is well under 240 so reconciler lag never drops a legitimately-in-flight PENDING.

**Q: Is recorded PnL gross or net, and where does the fee come from?**
Net of fees. `record_resolution` (WIN/LOSS/PUSH) subtracts ONLY the entry fee — winners redeem at $1 with no second trade — persisting `poly_entry_fee` and `pnl_gross`. The exception is TP/SL closes (`record_position_close`): both legs, `pnl_net = gross - entry_fee - exit_fee`. Fee model: `poly_fee_per_share(p) = 0.05·p·(1-p)`, peaking at p=0.50. `record_resolution` is idempotent/race-safe: re-reads the row, returns False on missing-or-already-terminal, guarded `UPDATE ... WHERE outcome='PENDING'` (rowcount==0 → False), so concurrent settlers can't double-count.

**Say it like this:** "We insert the bet as PENDING before talking to the exchange, so exposure counts from insert time and a crash mid-verify still leaves a record. Reconciliation never cancels a matched order — if CLOB shows a fill the ledger doesn't have, we recover it into a sentinel PENDING row, idempotent via the unique `order_id` index, and we refuse a recovery with mixed-side trades. Resolution subtracts only the entry fee; only TP/SL closes pay both legs."

### Resolution

**Q: You have local WU actuals. Why prefer Polymarket's resolution over your own ground truth?**
Because Polymarket is what actually pays out — any disagreement means *they* win the dispute. WU is the only trusted source for *betting* (calibration, gates), but for *settlement* Polymarket is authoritative. The ladder is `polymarket_winner → terminal_yes → terminal_token → _resolve_via_gamma_close` — the only active production path, since `EARLY_RESOLUTION_ENABLED=False`. `_resolve_via_gamma_close` requires every bracket `closed:True`, exactly one bracket at YES ≥ 0.995, and parseable bounds. The per-bracket Gamma path and the WU-actual fallback are NOT called by the tick — WU fallback is manual-only (CLI/dashboard), gated on `POLYMARKET_FALLBACK_DAYS=1`, writing a distinct `resolution_source='wu_actual_fallback'`. Resolution runs 24/7 but gates same-local-day dates until `RESOLUTION_SCAN_START_HOUR=18`.

**Q: When is a token declared lost?**
Asymmetric dead-token rule: a WIN needs `best_bid >= 0.995`, but a LOSS needs an *executable* `best_ask <= 0.005` — a missing ask is an information void (None), not a loss, so the row stays PENDING. NULL/zero/NaN fill prices are downgraded to PUSH (not loss), flagged `unrecorded_loss=true` when the bracket actually lost.

**Say it like this:** "Actuals come only from WU, stored in Celsius after a unit conversion keyed on the station's display unit, and the read side enforces `source IN ('wu')`. Settlement is Polymarket-first: in production we wait for Gamma to mark the event closed with exactly one bracket at 0.995. A win needs a 0.995 bid but a loss needs an *executable* 0.005 ask; a missing ask is a quote void, not a loss."

### Backtest Integrity

**Q: Why JSON, not pickle, for calibration params?**
`save_emos()` serializes the four EMOS coefficients plus `n_samples` as UTF-8 JSON into `params_blob`, and `load_emos()` reconstructs `EMOSParams` via `json.loads`, swallowing `(JSONDecodeError, KeyError)` to return None. JSON is version- and language-stable, human-inspectable, and can't execute arbitrary code on load the way pickle can — which matters for a record read by a long-lived money-moving process directly out of the production DB. The schema comment still says "pickled parameter object" — stale; the storage is JSON.

**Q: Why Parquet for the backtest decision table?**
It's a wide columnar dataset (per-hour prices, ladders, signal flavors) read repeatedly by sweeps; Parquet gives compressed columnar reads and preserves dtypes, and `build_decision_table` uses an mtime cache so it only rebuilds when a source DB is newer. The leakage gate runs before the write, so any consumer trusts an already-validated artifact.

**Q: How do you know the backtest isn't quietly leaking and overstating PnL?**
The structural guarantee is the one-way dependency: `src/hightempbot` never imports `backtest/`. The backtest mirrors src constants by literal value and tuned numbers flow back via `candidate_l2_depth.json → strategy_constants.py`, guarded by `tests/test_strategy_constants.py` (fails CI on drift). The leakage guarantee is `assert_no_leakage` before any Parquet write, with the EMOS-`<=`/LUT-strict-`<` asymmetry matching live. There's also the pandas datetime64 bug we hit: `astype('int64')//10**9` assumed nanoseconds (pandas ≤2.x) but pandas 3.x defaults to microseconds, making the value ~1000× too small and collapsing the per-hour leakage gate — fixed in two places via `.astype('datetime64[s]')`.

**Q: When backtest PnL doesn't match live, where do you look first?**
The bracket parser, not the model. On 2026-05-09 a three-way diagnostic isolated a bracket-bounds change (from an extends-to-next-edge rule to the ROUND-rule `[X-0.5, X+0.5)`): pre-fix BR100 train was +$124.52, the bracket-only fix dropped it to +$0.51, then aligning the sigma floor (0.5→0.1°C) moved it to +$3.46 — so the bracket parser dominates parity ~98%, sigma ~2%. The two parsers (`parse_bracket` on labels, `parse_bracket_bounds` on question text) consume different inputs but must produce identical `(lo, hi)`, and membership is half-open, so a half-degree edge shift directly flips boundary-case resolutions. Selection is expanding ABCD walk-forward where chunk A is train-only and never reported out-of-sample.

**Say it like this:** "The backtest is strictly downstream of live — it mirrors constants and feeds tuned values back through a JSON config guarded by a parity test. The anti-leakage gate raises before any Parquet is written and is deliberately asymmetric: EMOS params can be as-of the market date, LUT observations must be strictly before it. When parity diverges, check the bracket parser first — boundary-case settlement differences swamp Gaussian-width effects on this dataset."

### Dashboard / Ops

**Q: The dashboard binds `0.0.0.0` on bare HTTP in production. Isn't that a hole, and why doesn't the bot refuse to start?**
Asymmetric by design. `_validate_dashboard_live_safety` FAILS CLOSED (raises) only in LIVE mode on a non-loopback host lacking `dashboard_pass` or `dashboard_tls_terminated`. A separate, softer check only WARNS + alerts (`startup_degraded`) on non-loopback plaintext — it does NOT refuse — so current prod (`0.0.0.0` plaintext behind a firewall) keeps running. Mitigations: cookie auth (not Basic — HTMX/XHR don't reliably send Basic headers), constant-time `secrets.compare_digest` on a `token_hex(32)` cookie, a per-IP login rate limiter (5 failures/60s → 5-min lockout, keyed on `request.client.host` ignoring `X-Forwarded-For` so it can't be spoofed), and `/health` being the only unauth route (detailed metrics are auth-gated at `/api/v2/admin/health`). Honest caveat: on plain HTTP the cookie's `secure` flag is False, so deploys must sit behind an SSH tunnel/VPN/firewall.

**Q: A wallet/operator fetch fails while building the payload. Does the whole dashboard 500?**
No — two-tiered degradation in `_build_v2_payload`. The outer try wraps `build_htb_data`; if that raises you get the full `_degraded_v2_payload` (schema-complete with sentinels so the SPA shows an error banner). The inner try wraps `build_operator_wallet_payload`; if only that fails it overlays just `{operator, wallet, readiness, liveActionsEnabled}` and leaves trading data intact. Both degraded paths hardcode `liveActionsEnabled=False` and `wallet.actionsEnabled=False` — fail closed.

**Q: Is the "Max DD" card windowed by the range filter?**
No — "current" is load-bearing. `capital`, `peak`, `dd_pct`, `totalPnl`, `realizedPnl`, and the calendar are computed from the FIXED session floor `DASHBOARD_SESSION_START_UTC` regardless of the range filter (only KPI cards, charts, and the resolved-positions table window). Windowing the drawdown would hide the very drawdown the `MAX_DD=0.40` halt compares against. WIN/LOSS semantics are consistent: CLOSED + positive PnL is a WIN (TAIL TP exit), CLOSED + non-positive is a LOSS; per-fill rows stay row-exact, never aggregated. `liveActionsEnabled` is fail-closed True only when ALL of (not dry_run) AND readiness OK+fresh AND wallet actions enabled AND not `bootDryRun`. Operator mutations run through `assert_fresh_live_action_context` (except stop, always allowed), and transfer submit re-previews at a 30s TTL to close the preview→submit TOCTOU.

**Say it like this:** "Auth is cookie-based — open when no password is set, constant-time-compared when it is, with a per-IP rate limiter. The data endpoint degrades in two layers: a wallet failure only blanks the operator panel. Capital and drawdown always use the fixed session floor regardless of the range filter, because windowing would hide what the halt gate sees. Live actions are gated behind a fresh readiness report plus a fresh wallet snapshot."

## Trading & Stats Fundamentals (Section 2)

This section covers the statistical and trading definitions an interviewer will probe, anchored to exactly how they are coded in HighTempBot — not the textbook idealizations. Where the code diverges from the obvious form, the code wins.

### CRPS (Continuous Ranked Probability Score)

CRPS is the loss function HighTempBot minimizes when fitting EMOS. It is the scoring rule for a *full predictive distribution* against a single observed scalar. For a predictive CDF `F` and observation `y`, the integral form is:

```
CRPS(F, y) = ∫_{-∞}^{∞} ( F(x) − 1{x ≥ y} )² dx
```

i.e. the squared L2 distance between the forecast CDF and the Heaviside step function at the observation. Lower is better; it has temperature units (°C here), which makes it interpretable as "how far off, distribution-aware." For a Gaussian predictive distribution there is a closed form, and the code uses exactly that: in `calibration/emos.py::_crps_loss`, the objective is `np.mean(properscoring.crps_gaussian(actuals, mu=mu, sig=sigma))` — the mean Gaussian CRPS across all training pairs. `scipy.optimize.minimize` with `method="L-BFGS-B"`, `maxiter=500` minimizes this mean CRPS over the four EMOS parameters.

Why CRPS and not, say, log-loss on a bracket? CRPS rewards both calibration *and* sharpness simultaneously and is finite/robust even when the observation lands in the far tail (log-loss explodes there). It scores the whole Gaussian, which is what we then integrate to get per-bracket probabilities downstream.

### Proper scoring rule

A scoring rule is *proper* if the forecaster minimizes their expected score by reporting their true belief, and *strictly proper* if that true belief is the unique minimizer. This is the property that makes CRPS safe to optimize: the fitted Gaussian cannot game the loss by being deliberately over- or under-confident. CRPS is strictly proper; so is the Brier score (below). Improper rules (e.g. raw accuracy / "did the point forecast land in the bracket") would let a degenerate forecaster win by collapsing variance, which is precisely the failure mode the degenerate-sigma alarm guards against.

### The EMOS variance model — exact coded formula

EMOS (Ensemble Model Output Statistics) is non-homogeneous Gaussian regression: it dresses the raw 9-model ensemble into a calibrated predictive Gaussian `N(mu, sigma²)`. The EXACT coded formula (`calibration/emos.py`) is:

```
mu     = a + b * ensemble_mean
sigma  = sqrt( exp(c) + exp(d) * ensemble_var )
```

with a final floor: `sigma = sqrt(max(sigma², _SIGMA_FLOOR²))`.

Critical details an interviewer will dig into:

- **Log-space spread, on BOTH variance terms.** The variance is `sigma² = exp(c) + exp(d) * ensemble_var`, NOT `c + d * ensemble_var`, and NOT `exp()` of sigma itself. `c` is the log of the intercept variance; `d` is the log of the slope on ensemble variance. The `exp()` transform guarantees `sigma² > 0` for any optimizer state. A reader who assumes a linear-variance model, or who thinks `sigma` (rather than `sigma²`'s two components) is exponentiated, is wrong.
- **`mu` is a plain affine map** of the ensemble mean: `a` is a bias correction, `b` is a spread/scale correction on the ensemble mean.
- **Parameter bounds:** `bounds = [(None, None), (None, None), (-10, 10), (-10, 10)]`. `a` and `b` are *unconstrained*; only `c` and `d` are clamped to `[-10, 10]` to bound `exp()` against overflow (`exp(10) ≈ 22000`, ample for temperature variance in °C). Initial guess `x0 = [0.0, 1.0, 0.0, 0.0]` — an identity regression (`a=0, b=1`) with moderate spread (`exp(0)=1` for both variance terms).
- **`_SIGMA_FLOOR = 0.1` (°C).** It floors `sigma`, but is *implemented on the variance* as `np.maximum(sigma², _SIGMA_FLOOR**2)` = `max(sigma², 0.01)` before the sqrt. There is no `SIGMA_FLOOR_C` in `strategy_constants.py`; the floor lives only in `calibration/emos.py`.
- **`ddof=0` (population variance) everywhere** — both in fit (`ensemble_matrix.var(axis=1, ddof=0)`) and in `predict_emos` (`ensemble_members.var(ddof=0)`, with `0.0` when only one member). This is deliberate (Gneiting 2005): the 9 ensemble members ARE the forecast distribution sample, not a sample from an unknown population, so we use population variance, not the `ddof=1` sample-variance estimator.

Two safety behaviors worth naming: the **degenerate-fit alarm** recomputes the *fitted* per-row sigma and compares it to `_SIGMA_FLOOR + 1e-6` (≈ 0.100001); if every training pair collapses to the floor it logs CRITICAL ("calibration is degenerate; downstream probabilities will be over-confident"), but it is log-only — it does NOT abort or alter params. (It explicitly does NOT compare the log-space parameter `d` to the floor — different scale.) And **non-convergence is fail-soft**: `result.success == False` logs an error but still returns `result.x` ("partial convergence is usually usable"). `fit_emos` returns `None` ONLY when `n_days < 10`.

The exceedance probability that feeds betting is `emos_probability = 1 − Φ((threshold − mu)/sigma)` = `P(tmax > threshold)`, a strict upper-tail with no continuity correction.

### VWAP (Volume-Weighted Average Price)

VWAP is the realized average fill price across the order-book levels the walker actually consumed: `filled_usd / filled_shares`. The bot never assumes the top-of-book price; it walks the ask ladder cheapest-first and recomputes VWAP after each level (`execution/walker.py::walk_book_edge_preserving`). The realized edge is computed against this VWAP, not against the displayed best ask — so a thin book that forces the walker deeper degrades the realized edge, and the walker *stops* the instant the marginal new VWAP would push realized edge below the floor. VWAP also serves as the sticky slot anchor for cross-tick top-ups (`slot_first_fill_vwap`).

### Edge — NO and YES formulas

Edge is the expected per-share profit after the Polymarket taker fee. The fee model is `poly_fee_per_share(price) = θ · p · (1−p)` with `θ = POLY_FEE_THETA = 0.05` (peaks at `p = 0.50`).

- **NO edge:** `edge = (1 − p) − fill_price − fee`, where `fill_price` is the NO token's OWN executable price — not the YES quote's best bid. In `scheduler/market_data.py::_build` the dict key `best_bid` is set to `no_price = prices[1]` — the actual NO outcome price from Gamma (`float(prices[1])`, with a `1 − yes_price` fallback used only when `prices[1]` is absent). At enrichment time `scheduler/betting_tick.py` (lines 472–485) overwrites it with `reader.fetch_price(no_token, "buy")` — the executable BUY price on the NO token itself — or, if that price is missing, the NO book's best ask. The NO walk then consumes `_no_book` (`decision/strategies.py:1005`, `book_key = "_no_book"`), the NO token's own ask ladder. The key is *labeled* `best_bid` for historical reasons, but it holds the NO token's executable price, never the YES token's bid. `prob_safe_floor = 1 − p` is the empirical *miss* rate: NO is buying the "it won't land in this bracket" side, so payoff is `(1 − p)`.
- **YES edge:** `edge = p − fill_price − fee`, where `fill_price` is `best_ask` (the YES token's ask) and `prob_safe_floor = p`.

`_fee_adjusted_edge` is structurally identical for both sides: `prob_safe_floor − price − fee`. The asymmetry is entirely in which probability and which book side feed it.

### The two-sided edge band

The edge gate is two-sided and the two sides are *not* mirror images:

- **NO uses a POSITIVE band.** The live `NO` config requires `edge ∈ [min_edge = 0.090, max_edge = 0.15]` (strict path), relaxed to `≤ 0.35` on the ceiling-bracket extension. The `max_edge` ceiling is a model-error tripwire: an implausibly large claimed edge usually means the model, not the market, is wrong.
- **YES uses a NEGATIVE band — by design.** The legacy `YES_MIN_EDGE = -0.10`, `YES_MAX_EDGE = -0.03`. Negative-edge YES is contrarian and intentional, not a bug; it is buying favorites where the market is slightly underpricing relative to the calibrated model. (These `YES_*` globals were legacy single-gate constants, removed from `strategy_constants.py` 2026-08-09; the live router reads `STRATEGY_CONFIGS`. But the *band semantics* — NO positive, YES negative — are the conceptual point.)

Note the global `MIN_EDGE = 0.03` / `MAX_EDGE = 0.10` are **not** the live NO band; the router uses the per-strategy `NO.min_edge = 0.090` / `NO.max_edge = 0.15`. Don't quote the globals as the live thresholds.

### FAK vs FOK vs GTC — the bot uses FAK for entries

- **FAK (Fill-And-Kill):** fill as much as is immediately available at the limit, cancel the remainder. No resting order. The bot places **entries** with `OrderType.FAK` (`execution/walker.py::OrderClient.place_order`). FAK matches the edge-preserving walk: take what the book gives at acceptable prices, drop the rest — a partial fill is fine and is reconciled into the ledger from the actual matched trades.
- **FOK (Fill-Or-Kill):** all-or-nothing, immediate. The bot uses `OrderType.FOK` ONLY for **position closes** (`close_position`), because a TP/SL exit must be all-or-nothing — a partial exit must never be booked as "closed."
- **GTC (Good-Till-Cancelled):** rests on the book until filled or cancelled. The bot does **not** use GTC for trading; it never wants to leave a resting maker order exposed to adverse selection on a 1-day weather market.

So: FAK entries, FOK closes, no GTC. `place_order` is also explicitly *not* idempotent, which is why `execute_or_log` verifies an existing order on retry rather than re-submitting.

### Kelly criterion — and why it was removed

The Kelly criterion sizes a bet as the fraction of bankroll that maximizes expected log-growth: `f* = edge / odds` (for a binary bet, `f* = p − (1−p)/b`). It is growth-optimal in theory but notoriously fragile to estimation error in `p` — and a weather-market edge estimate has real model error, so full Kelly massively over-bets and fractional-Kelly tuning is unstable.

HighTempBot **removed Kelly on 2026-04-24** in favor of fixed capital fractions per strategy. Sizing is now simply `target_usd = capital × capital_frac`, filled edge-preservingly across the book. The live fractions (verified):

- **NO: `capital_frac = 0.07`** (7% of capital; 2026-06-05 operator override).
- **TAIL: `capital_frac = 0.050`** (5%).

There is no static per-bet dollar cap and no Kelly multiplier; exposure scales linearly with bankroll. (The ledger still writes the same value to both `kelly_size` and `bet_size` columns for backward compatibility — `bet_size = kelly_size = signal.bet_size_usd` — but the `kelly` name is vestigial.) Drawdown is governed separately by a hard halt at `MAX_DD = 0.40`: when realized capital falls 40% below peak realized capital, `run_betting_cycle` places no new bets that tick (existing PENDING still resolve); the old "halve target at MAX_DD" reduced-size band was removed.

### Brier score / BSS — and the anti-correlation gotcha

The **Brier score** is the mean squared error of probabilistic forecasts against binary outcomes: `Brier = mean( (p_i − o_i)² )`, where `o_i ∈ {0,1}`. Lower is better; it is a proper scoring rule (the binary special case of CRPS). The **Brier Skill Score (BSS)** normalizes it against a reference (climatology): `BSS = 1 − Brier_model / Brier_reference`. `BSS = 1` is perfect, `0` is no better than climatology, negative is worse.

The non-obvious, load-bearing gotcha for this system: **per-station BSS is stable but ANTI-correlated with PnL.** A high-BSS station means the model is accurate there — but if the model is accurate, the *market* is usually accurate too, so there is no mispricing to exploit and no edge. Per-station PnL itself is essentially noise (Spearman ≈ 0), and `Spearman(h1_BSS, h2_PnL) ≈ −0.27`. The operational conclusion, baked into the code, is: **do not filter live stations by PnL OR by BSS.** The legacy BSS gate was retired (Phase F, 2026-04-22), and the live decision path uses `prob_safe_floor` (the raw LUT bucket hit rate) — Wilson confidence bands and BSS-based station qualification are gone. An interviewer who expects "rank stations by skill and trade the best ones" is reasoning exactly backwards for this market.

## Bug War Stories (Section 1)

These are real bugs that shipped (or nearly shipped) and what they taught us about this codebase. Each is in STAR format. The recurring theme: in a calibrated-probability betting bot, a one-line numeric or unit error doesn't crash — it silently biases the *probability* the whole pipeline is built on, and the symptom shows up three layers downstream as "the strategy is broken."

### A. The all-NO-bet sigma floor (the forecast floor was too wide)

**Situation.** Every betting cycle was emitting NO bets on essentially every bracket and almost no YES bets. The bot believed it had a systematic edge on the "NO" side of every market — which is exactly what a too-confident-the-wrong-way calibration looks like.

**Task.** Find why the model's true probabilities were systematically far below the market's implied probabilities (true_prob ≈ 22% where the market sat ≈ 48%), manufacturing a fake NO edge on bracket after bracket.

**Action.** The root cause was a forecast-side sigma *floor* set far too wide. The historical forecast archive being trained on was near-analysis (near-D+0) data, which makes the fitted spread artificially small; the wide floor was overcompensating, inflating the predictive Gaussian's width so the model assigned too little mass inside each bracket and too much to "NO." The floor was on the order of 2–4× the real day-ahead sigma. The fix lowered that forecast floor dramatically and added a confirmation guard so a YES edge required a second source to agree before placing.

**Crucial distinction for anyone reading the current code.** There are two different "sigma floors," and conflating them is the trap. The one in this story was a *forecast-side* floor (historically `SIGMA_FLOOR_C`, a config constant). In today's codebase there is **no `SIGMA_FLOOR_C` constant in `execution/strategy_constants.py` at all** — I grepped it to be sure. The only sigma floor that exists now is the numerical guard inside the EMOS fit/predict path: `calibration/emos.py::_SIGMA_FLOOR = 0.1` (°C), applied as `sigma = sqrt(max(sigma2, _SIGMA_FLOOR ** 2))` in `fit_emos`, `predict_emos`, and the degenerate-fit alarm. That 0.1 floor is a numerical-stability guard (so `sigma > 0` regardless of optimizer state), *not* the calibration-width knob that caused the all-NO pattern. So when someone says "the sigma floor caused all-NO bets," they mean the old forecast-width floor — `_SIGMA_FLOOR = 0.1` in `emos.py` is a different animal and, as story C shows, rarely even binds.

**Result.** Cycles went from all-NO back to a healthy mix of YES and NO, and the spurious NO edges disappeared.

**Lesson.** A sigma floor is a probability-calibration knob in disguise. Set it wider than the market's real uncertainty and you don't get "conservative" — you get systematically wrong-side fake edges on every bracket. When every bet lands on one side, suspect calibration width before suspecting the strategy gates.

### B. Pandas 3 datetime64 unit — silent leakage gate collapse

**Situation.** After a pandas upgrade, the backtest's per-hour entry stream started letting through far more candidates than it should have, inflating bet counts and PnL. Nothing errored.

**Task.** Figure out why the entry-time leakage gate — the per-hour `not_leakage` mask that requires an entry snapshot timestamp be at or *after* the market date's midnight (`entry_ts >= md_unix`), rejecting any entry sourced from *before* the market date — had silently stopped firing. (Note the direction: this mask guards against pulling a snapshot from a day earlier than the market, not against entering too late. The separate too-close-to-close guard — `entry_ts + hours_before_close*3600 > close_ts` — lives in `backtest/lib/sweep_lib.py::assert_no_leakage`, not in this per-hour mask.)

**Action.** The code computed Unix seconds from a `datetime64` column with `astype("int64") // 10**9`. That math assumes the column's unit is **nanoseconds**, which was true on pandas ≤ 2.x. Pandas 3.x changed the default `datetime64` resolution to **microseconds**, so `astype("int64")` now yields microseconds; dividing by `10**9` produced a value ~1000× too small. In `not_leakage = ~np.isnan(entry_ts) & (entry_ts >= md_unix)`, `md_unix` was then derived ~1000× smaller than the real Unix-second midnight cutoff, so `entry_ts >= md_unix` was satisfied by nearly every row and the gate collapsed — almost everything passed as "not leakage." The fix forces the unit explicitly: `pd.to_datetime(...).astype("datetime64[s]").astype("int64")`, giving true Unix seconds regardless of pandas version. This fix lives in **two** places that both build per-hour entry timestamps — `backtest/lib/sweep_lib.py::evaluate_config()` (the `entry_h is not None` branch) and `backtest/lib/live_match_eval.py::_entry_arrays()` (the `hour is not None` branch) — and both had to be patched. (The default no-hour path is unaffected because its `not_leakage` mask is just `~np.isnan(entry_ts)`; the `md_unix` comparison only runs for explicit per-hour entries.)

**Result.** The leakage gate fired correctly again, bet counts and PnL returned to honest levels, and `assert_no_leakage` (the hard date-ordering gate in `sweep_lib.py`) stayed meaningful.

**Lesson.** Never assume a `datetime64` is in nanoseconds — cast to `datetime64[s]` before going to `int64`. A unit assumption that is correct on one library version becomes a silent data-leakage bug on the next, and a leakage bug that *inflates* results is the most dangerous kind because the numbers look great. Also: when a fix touches a shared computation, grep for every copy — this one had two.

### C. The bracket parser dominates backtest/live parity (+0.5°F bound shift)

**Situation.** A backtest result diverged from what live would do, and the instinct was to blame the EMOS Gaussian — specifically the sigma floor, since the backtest mirror of `_SIGMA_FLOOR` had been wrong (5× too wide) until it was corrected to match live's `0.1`.

**Task.** Attribute the parity gap correctly instead of guessing, by reverting one suspected bug at a time.

**Action.** The bracket-bound parser had been using an "extends-to-next-edge" rule that shifted bounds by +0.5°F at every bracket edge, instead of the ROUND-rule semantics live uses (`resolution/gamma.py::parse_bracket_bounds`, where label X covers continuous actual `[X-0.5, X+0.5)`). A three-way diagnostic isolated the contributions cleanly: fixing **only** the bracket parser closed the overwhelming majority of the train-PnL gap, and additionally aligning the sigma floor (0.5 → 0.1°C to match live) closed only a small remainder. The +0.5°F shift specifically changed which bracket a boundary-case actual — one falling in the disputed half-degree band — "won," and that flips a WIN to a LOSS for those bets. Sigma floor barely moved anything because most fitted EMOS sigmas sit well above 0.1°C, so the floor rarely binds and changing it shifts almost no Gaussian integration mass.

**Result.** Backtest and live converged. The lopsided contribution (bracket parser ≫ sigma floor) was quantified rather than assumed.

**Lesson.** When a backtest diverges from live, check `parse_bracket` (label input) vs `parse_bracket_bounds` (question-text input) **first** — the two parsers consume different inputs but must produce identical `(lo, hi)` bounds, and bracket-membership is half-open `[lo, hi)`, so a half-degree edge shift directly flips boundary-case resolutions. Boundary-case settlement differences swamp Gaussian-width effects on this dataset. Don't reach for the calibration math until you've ruled out the parser, and verify with a revert-one-bug-at-a-time diagnostic instead of fixing two things and claiming credit.

### D. P0 Kelly denominator — NO was systematically undersized

**Situation.** This was the P0 finding from a full review: the bot was using the same Kelly denominator for both sides. The code computed size with `edge / (1 - fill_price)` for *both* YES and NO bets.

**Task.** Determine the correct side-aware Kelly sizing and quantify the error.

**Action.** The Kelly denominator must differ by side: a YES bet at price `p` risks `p` to win `1-p`, so its denominator is `(1 - fill_price)`; a NO bet at price `p` risks `p` to win `1-p` on the complement, so its denominator is `fill_price`. Using `(1 - fill_price)` for NO was simply wrong — for a NO fill at 0.40 the correct denominator is 0.40 (you risk 0.40 to win 0.60), not 0.60. Every NO bet was therefore sized at roughly two-thirds of correct Kelly: systematic undersizing of the entire NO sleeve. The fix split the two paths and also checked the VWAP walk-the-book path, which carried its own copy of the sizing math.

**Result.** NO bets were sized correctly side-for-side. (Historical note: Kelly sizing itself was later removed entirely on 2026-04-24 — the current bot sizes by `capital * capital_frac` per strategy, filled edge-preservingly across the book, with no Kelly term. So this story is about the era before that refactor, but the lesson outlived the mechanism.)

**Lesson.** Anything that is asymmetric between YES and NO — Kelly denominator, fill-price source (`best_ask` for YES vs `best_bid` for NO), prob_safe_floor (observed rate for YES vs `1 - observed` for NO), the edge band — must be implemented as two explicit paths, and you must check every place that recomputes it (the walk-the-book path had a duplicate). A "shared" formula that happens to be right for one side is silently wrong for the other, and undersizing doesn't throw an error — it just quietly leaves money on the table on half your trades.

## What should you say if asked exact constants?

Always point at `execution/strategy_constants.py` as the single source of truth, and say so out loud. Every tuned number — sizing, gates, cadences, thresholds — lives there as a module-level literal, and the live 5-sleeve router reads `STRATEGY_CONFIGS`, not the legacy single-gate globals. The values were ported from `backtest/configs/candidate_l2_depth.json` (variant `sel_taila40_fp03_cs40`) on 2026-05-29, and `tests/test_strategy_constants.py` is a CI parity test that fails the build if any ported value drifts from that JSON. So the honest answer to "what is the NO edge band?" is: cite the file, then the value.

The high-value ones to have memorized, and the traps:

- **Sizing / halt:** `MIN_BET_USD = 1.0`, `MAX_DD = 0.40` (drawdown halt — suspends *new* placement, existing PENDING still resolve; the old "halve target" band and Kelly are both gone), `MAX_PENDING_EXPOSURE_PCT = 1.00`, `POLY_FEE_THETA = 0.05`. Per-bet size is `capital * capital_frac` (NO `0.07`, TAIL `0.050`), filled edge-preservingly across the book — there is no static dollar cap.
- **The router does NOT use `MIN_EDGE = 0.03` / `MAX_EDGE = 0.10`.** NO's actual additive band is `min_edge = 0.090`, `max_edge = 0.15`. The globals (and `NO_MIN_FILL_PRICE`, `YES_*`) existed only for regression tests and were removed from `strategy_constants.py` 2026-08-09. If you quote 0.03 as the live NO floor you're wrong.
- **Execution edge floors are per-strategy and distinct from the gate edge:** NO `execution_min_edge = 0.05`, TAIL `0.07` — these are realized-VWAP floors enforced by the walker; the 0.05 `max_walk_price` leash is bypassed for both because `execution_min_edge` is set.
- **Cadence is 10 minutes, not 15.** `SCAN_INTERVAL_MINUTES = 10`; the only true 15-min job is the hardcoded `system_health_check` (`minute="5,20,35,50"`). Trust the constant, not the docstrings.
- **Three different 30s:** `MIN_BUCKET_SAMPLES` (removed 2026-08-09), `MIN_PAIRS`, `LUT_MIN_N_FOR_SHRINKAGE` all equalled 30 but meant different things (bucket trust / calibration readiness / shrinkage + TAIL `vote_min_n`). The binding cold-start gate is `LUT_MIN_N_FOR_SHRINKAGE`.
- **Disabled-by-value flags:** `BETTING_LOCAL_CUTOFF_HOUR = 0` *disables* the cutoff (set 14 to enable); `WU_CONSENSUS_MODE = "OFF"` (hardcoded, not env-overridable); `EARLY_RESOLUTION_ENABLED = False`; `POLYMARKET_FALLBACK_DAYS = 1`. Two strategies (YMID, YHIGH) are `enabled=False` — only NO and TAIL fire.
- **TAIL specifics:** `fp_max = 0.03` but `delayed_entry_fp_max = 0.02` (gate eligibility on the wider band, but can't place until ask ≤ 0.02), `alpha_ratio = 4.0` per-voter, 4-of-4 unanimous vote, `consensus_skip_threshold = 0.40`, `tp = 0.20`, entry hour `{1}`.
- **One pointer outside the file:** the EMOS sigma floor is `_SIGMA_FLOOR = 0.1` °C in `calibration/emos.py` — there is no `SIGMA_FLOOR_C` in `strategy_constants.py`. Don't invent one.

If you don't remember an exact value, say "it's a literal in `strategy_constants.py` (or `candidate_l2_depth.json`)" rather than guessing — confidently citing the source beats a wrong number.

