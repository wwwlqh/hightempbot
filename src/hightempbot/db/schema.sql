-- Observed daily max temperature per station
CREATE TABLE IF NOT EXISTS actuals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id      TEXT NOT NULL,           -- ICAO code (e.g. KLGA)
    local_date      TEXT NOT NULL,           -- ISO date YYYY-MM-DD in station local time
    tmax_celsius    REAL NOT NULL,           -- daily max in °C
    source          TEXT NOT NULL,           -- "wu"
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(station_id, local_date)
);
CREATE INDEX IF NOT EXISTS idx_actuals_station_date ON actuals(station_id, local_date);

-- Forecast archive: one row per (station, target_date, horizon, model)
CREATE TABLE IF NOT EXISTS forecast_archive (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id      TEXT NOT NULL,
    target_date     TEXT NOT NULL,           -- local date being forecast
    horizon         INTEGER NOT NULL,        -- 1, 2, or 3 (days ahead)
    issue_date      TEXT NOT NULL,           -- UTC date of the model run
    centre          TEXT NOT NULL,           -- model name (e.g. "ecmwf_ifs025", "gfs_seamless")
    member          INTEGER NOT NULL,        -- always 0 (deterministic models)
    tmax_celsius    REAL NOT NULL,           -- forecast tmax in °C
    source          TEXT NOT NULL DEFAULT 'openmeteo',  -- "openmeteo" or "live"
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(station_id, target_date, horizon, centre, member, source)
);
CREATE INDEX IF NOT EXISTS idx_forecast_station_date
    ON forecast_archive(station_id, target_date, horizon);

-- Calibration parameters per (station, horizon, threshold_bucket)
CREATE TABLE IF NOT EXISTS calibration_params (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id      TEXT NOT NULL,
    horizon         INTEGER NOT NULL,
    threshold_bucket REAL,                   -- NULL for EMOS-level params
    param_type      TEXT NOT NULL,           -- "emos" or "isotonic"
    params_blob     BLOB NOT NULL,           -- pickled parameter object
    n_samples       INTEGER NOT NULL,
    trained_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(station_id, horizon, threshold_bucket, param_type)
);
CREATE INDEX IF NOT EXISTS idx_cal_station_horizon
    ON calibration_params(station_id, horizon);

-- One (prediction bucket, hit) triple per station, date and bracket; rebuild_lut
-- aggregates these into lut_bucket_stats.
CREATE TABLE IF NOT EXISTS pred_bucket_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id        TEXT NOT NULL,
    local_date        TEXT NOT NULL,
    pred_bucket_low   REAL NOT NULL,
    pred_bucket_high  REAL NOT NULL,
    bracket_low       REAL,
    bracket_high      REAL,
    bracket_key       TEXT,
    emos_p            REAL NOT NULL,
    hit               INTEGER NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(station_id, local_date, bracket_key)
);
CREATE INDEX IF NOT EXISTS idx_pred_bucket_station_date
    ON pred_bucket_history(station_id, local_date);
CREATE INDEX IF NOT EXISTS idx_pred_bucket_cell
    ON pred_bucket_history(station_id, pred_bucket_low);

-- LUT (lookup table) per-bucket empirical hit-rate calibration.
CREATE TABLE IF NOT EXISTS lut_bucket_stats (
    station_id        TEXT NOT NULL,
    pred_bucket_low   REAL NOT NULL,
    pred_bucket_high  REAL NOT NULL,
    n                 INTEGER NOT NULL DEFAULT 0,
    hits              INTEGER NOT NULL DEFAULT 0,
    observed          REAL,                          -- hits/n when n > 0; NULL otherwise
    mean_pred         REAL,                          -- mean EMOS p across triples in bucket
    refreshed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (station_id, pred_bucket_low)
);
CREATE INDEX IF NOT EXISTS idx_lut_stats_station ON lut_bucket_stats(station_id);
CREATE INDEX IF NOT EXISTS idx_lut_stats_refreshed ON lut_bucket_stats(refreshed_at);

-- Historical walk-forward EMOS params (one row per station, horizon, asof_date).
CREATE TABLE IF NOT EXISTS calibration_params_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id      TEXT NOT NULL,
    horizon         INTEGER NOT NULL,
    asof_date       TEXT NOT NULL,                   -- ISO YYYY-MM-DD the EMOS was fit for
    params_blob     BLOB NOT NULL,
    n_samples       INTEGER NOT NULL,
    computed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(station_id, horizon, asof_date)
);
CREATE INDEX IF NOT EXISTS idx_cal_history_station_date
    ON calibration_params_history(station_id, horizon, asof_date);

-- Live bet ledger
CREATE TABLE IF NOT EXISTS ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_ts          TEXT NOT NULL,           -- ISO8601 placement timestamp
    station_id      TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    target_date     TEXT NOT NULL,
    horizon         INTEGER NOT NULL,
    threshold       REAL NOT NULL,
    side            TEXT NOT NULL,           -- "YES" or "NO"
    p_model         REAL NOT NULL,
    p_market        REAL NOT NULL,
    edge            REAL NOT NULL,
    kelly_size      REAL NOT NULL,
    volume_cap      REAL NOT NULL,
    bet_size        REAL NOT NULL,
    limit_price     REAL NOT NULL,
    order_id        TEXT,
    fill_price      REAL,
    fill_size       REAL,
    fill_ts         TEXT,
    actual_tmax     REAL,
    outcome         TEXT,                    -- "WIN", "LOSS", "PUSH", "PENDING"
    pnl             REAL,
    kelly_multiplier REAL,
    event_type      TEXT DEFAULT 'bet',      -- "bet", "kelly_adjust", "degrade_flag"
    event_detail    TEXT,                    -- JSON for adjustment events
    realized_edge   REAL,                    -- post-fill edge at VWAP; NULL on dry_run / pre-fill
    -- Decision-time forensics
    prob_safe_floor   REAL,                  -- side-aware LUT-calibrated probability used in edge calc
    pred_bucket_low   REAL,                  -- matches lut_bucket_stats.pred_bucket_low
    pred_bucket_high  REAL,
    n_bucket          INTEGER,               -- sample count behind the bucket calibration
    -- Order verification
    transaction_hash         TEXT,           -- on-chain proof; "DRY_RUN_<uuid>" on dry-run
    verify_attempts          INTEGER DEFAULT 0,   -- 1..MAX_ORDER_RETRIES
    verification_downgraded  INTEGER DEFAULT 0,   -- 1 when MATCHED without tx_hash across all attempts
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_station ON ledger(station_id, bet_ts);
CREATE INDEX IF NOT EXISTS idx_ledger_outcome ON ledger(outcome);
CREATE INDEX IF NOT EXISTS idx_ledger_target_date ON ledger(target_date, outcome);
-- One row per Polymarket order (NULL order_ids exempt). init_db refuses to start
-- on duplicates; see scripts/check_ledger_order_id_duplicates.py.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_order_id
    ON ledger(order_id) WHERE order_id IS NOT NULL;
-- For the dashboard's windowed aggregates.
CREATE INDEX IF NOT EXISTS idx_ledger_event_outcome_bet_ts
    ON ledger(event_type, outcome, bet_ts);

-- Signal log: one row per (station, target_date, bracket, side) evaluated per tick
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,             -- ISO8601 UTC tick timestamp
    station_id      TEXT NOT NULL,
    target_date     TEXT NOT NULL,
    bracket_label   TEXT NOT NULL,             -- e.g. "66-68°F YES"
    side            TEXT NOT NULL,             -- "YES" or "NO"
    p_model         REAL,
    p_fill          REAL,                      -- market fill price
    edge            REAL,
    kelly_size_usd  REAL,
    volume_usd      REAL,                      -- nullable (tail brackets)
    gate_bss        INTEGER,                   -- 1=pass, 0=fail, NULL=not evaluated
    gate_edge       INTEGER,
    gate_fill_price INTEGER,
    gate_volume     INTEGER,
    gate_daily_exposure INTEGER,
    gate_idempotency INTEGER,                  -- 1=pass (new bet), 0=duplicate, NULL=not evaluated
    passed_all_gates INTEGER NOT NULL DEFAULT 0,
    outcome         TEXT NOT NULL              -- "BET", "WOULD_BET", "SKIP:<gate>"
);
CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_station_date ON signals(station_id, target_date);

-- Pipeline health events for dashboard monitoring
CREATE TABLE IF NOT EXISTS pipeline_health (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    stage           TEXT NOT NULL,             -- "actuals", "forecast", "calibration", "order", "resolution", "reconciliation"
    station_id      TEXT,                      -- NULL for system-wide events
    status          TEXT NOT NULL,             -- "OK", "WARNING", "ERROR", "SKIPPED", "DEGRADED", "ONBOARDING"
    message         TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_created_stage ON pipeline_health(created_at, stage);
CREATE INDEX IF NOT EXISTS idx_health_station_stage_time ON pipeline_health(station_id, stage, created_at DESC);

-- Auto-enrolled stations from Polymarket discovery
CREATE TABLE IF NOT EXISTS enrolled_stations (
    icao            TEXT PRIMARY KEY,
    city            TEXT NOT NULL,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    timezone        TEXT NOT NULL,
    unit            TEXT NOT NULL,              -- "F" or "C"
    resolution_source TEXT NOT NULL,            -- "wu"
    calibration_source TEXT NOT NULL,           -- legacy; unused by code, kept for DB compat
    poly_slug       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'DISCOVERED',  -- DISCOVERED/CONFIGURING/BACKFILLING/TRAINING/DRY_RUN/LIVE/SKIPPED/PAUSED
    step            TEXT,                       -- current sub-step within status
    step_detail     TEXT,                       -- progress info or error message
    skip_reason     TEXT,
    enrolled_at     TEXT NOT NULL DEFAULT (datetime('now')),
    dry_run_start   TEXT,
    bss             REAL,
    coverage_pct    REAL,
    actuals_count   INTEGER NOT NULL DEFAULT 0,
    forecast_count  INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Monthly retrain history
CREATE TABLE IF NOT EXISTS retrain_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id      TEXT NOT NULL,
    horizon         INTEGER NOT NULL DEFAULT 1,
    retrain_month   TEXT NOT NULL,              -- "2026-03"
    kept            TEXT NOT NULL,              -- "ok" or "skip"
    reason          TEXT,                       -- human-readable explanation
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(station_id, horizon, retrain_month)
);

-- Cached market token IDs from Polymarket (lazy-filled on first betting tick per station+date)
CREATE TABLE IF NOT EXISTS market_tokens (
    station_id    TEXT NOT NULL,
    market_date   TEXT NOT NULL,          -- ISO date of the temperature market
    bracket_idx   INTEGER NOT NULL,
    token_id      TEXT NOT NULL,          -- YES token (clobTokenIds[0])
    no_token_id   TEXT NOT NULL,          -- NO token (clobTokenIds[1])
    market_id     TEXT NOT NULL,          -- conditionId
    bracket_label TEXT,                   -- e.g. "62-63°F"
    bracket_low   REAL,
    bracket_high  REAL,
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (station_id, market_date, bracket_idx)
);

-- Wallet balance readings (audit history), one per successful live read.
CREATE TABLE IF NOT EXISTS bankroll_peak (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at      TEXT NOT NULL DEFAULT (datetime('now')),
    wallet_balance  REAL NOT NULL,         -- pUSD available collateral, USD
    realized_pnl    REAL,                  -- ledger SUM(pnl) at sample time (audit)
    pending_exposure REAL                  -- ledger SUM(PENDING bet_size) at sample time (audit)
);
CREATE INDEX IF NOT EXISTS idx_bankroll_peak_sampled_at
    ON bankroll_peak(sampled_at);

-- Shared live readiness reports. Payload is redacted JSON only: no private
-- keys, API secrets, passphrases, or relayer keys.
CREATE TABLE IF NOT EXISTS live_readiness_reports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    status              TEXT NOT NULL,
    mode                TEXT NOT NULL,
    funder              TEXT,
    signer              TEXT,
    clob_balance_usd    REAL,
    chain_balance_usd   REAL,
    report_json         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_readiness_created
    ON live_readiness_reports(created_at DESC);

-- Operator state (single row). A new DB starts STOPPED_PROCESSING; Start
-- can't override DRY_RUN.
CREATE TABLE IF NOT EXISTS operator_control_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    state           TEXT NOT NULL DEFAULT 'STOPPED_PROCESSING',
    boot_dry_run    INTEGER NOT NULL DEFAULT 1,
    reason          TEXT,
    updated_by      TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- Optimistic concurrency: UPDATE ... WHERE version = ?, then version + 1.
    version         INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO operator_control_state
    (id, state, boot_dry_run, reason, updated_by, version)
VALUES
    (1, 'STOPPED_PROCESSING', 1, 'initial schema state — fresh install halt', 'system', 0);

CREATE TABLE IF NOT EXISTS operator_control_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    action          TEXT NOT NULL,
    from_state      TEXT,
    to_state        TEXT,
    actor           TEXT,
    reason          TEXT,
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_operator_events_created
    ON operator_control_events(created_at DESC);

-- Wallet-first reconciliation snapshots keyed by the configured POLY_FUNDER.
CREATE TABLE IF NOT EXISTS wallet_reconciliation_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at          TEXT NOT NULL DEFAULT (datetime('now')),
    wallet_address      TEXT NOT NULL,
    source_status       TEXT NOT NULL,
    clob_balance_usd    REAL,
    chain_balance_usd   REAL,
    open_orders_count   INTEGER NOT NULL DEFAULT 0,
    open_positions_count INTEGER NOT NULL DEFAULT 0,
    warnings_json       TEXT NOT NULL DEFAULT '[]',
    snapshot_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_wallet_recon_wallet_time
    ON wallet_reconciliation_runs(wallet_address, sampled_at DESC);

CREATE TABLE IF NOT EXISTS wallet_reconciliation_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL,
    source_layer    TEXT NOT NULL,
    source_id       TEXT,
    record_type     TEXT NOT NULL,
    wallet_address  TEXT NOT NULL,
    order_id        TEXT,
    tx_hash         TEXT,
    token_id        TEXT,
    amount_usd      REAL,
    status          TEXT,
    matched_ledger_id INTEGER,
    match_status    TEXT NOT NULL DEFAULT 'unmatched',
    record_json     TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(run_id) REFERENCES wallet_reconciliation_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_wallet_records_run
    ON wallet_reconciliation_records(run_id);
CREATE INDEX IF NOT EXISTS idx_wallet_records_wallet
    ON wallet_reconciliation_records(wallet_address, source_layer, record_type);

CREATE TABLE IF NOT EXISTS transfer_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    actor           TEXT,
    from_wallet     TEXT NOT NULL,
    to_wallet       TEXT NOT NULL,
    amount_usd      REAL NOT NULL,
    status          TEXT NOT NULL,
    confirmation    TEXT,
    relayer_tx_id   TEXT,
    tx_hash         TEXT,
    error           TEXT,
    preview_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_transfer_requests_created
    ON transfer_requests(created_at DESC);
-- At most one in-flight transfer per wallet.
CREATE UNIQUE INDEX IF NOT EXISTS transfer_requests_single_submit
    ON transfer_requests(from_wallet) WHERE status='SUBMITTING';

CREATE TABLE IF NOT EXISTS redemption_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    actor           TEXT NOT NULL DEFAULT 'auto_redeemer',
    wallet_address  TEXT NOT NULL,
    condition_id    TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    outcome_index   INTEGER NOT NULL,
    index_set_value INTEGER NOT NULL,
    negative_risk   INTEGER NOT NULL DEFAULT 0,
    size            REAL NOT NULL,
    current_value_usd REAL,
    status          TEXT NOT NULL,
    matched_ledger_ids_json TEXT NOT NULL DEFAULT '[]',
    relayer_tx_id   TEXT,
    tx_hash         TEXT,
    error           TEXT,
    response_json   TEXT NOT NULL DEFAULT '{}',
    position_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_redemption_requests_created
    ON redemption_requests(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_redemption_requests_wallet
    ON redemption_requests(wallet_address, condition_id, token_id);
CREATE UNIQUE INDEX IF NOT EXISTS redemption_requests_active_unique
    ON redemption_requests(wallet_address, condition_id, token_id)
    WHERE status IN ('SUBMITTING','SUBMITTED','CONFIRMED');

-- NO-gate reliability curves (calibration/reliability.py); the latest active
-- row per group_key ('NO_C'/'NO_F') is used.
CREATE TABLE IF NOT EXISTS reliability_curves (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key   TEXT NOT NULL,
    fitted_at   TEXT NOT NULL,
    n_pairs     INTEGER NOT NULL,
    curve_json  TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_reliability_group_active
    ON reliability_curves(group_key, is_active, id DESC);

-- Top of book per bracket per betting tick, kept 180 days for refits and
-- live/backtest parity. snapped_at is SQLite UTC text.
CREATE TABLE IF NOT EXISTS book_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    snapped_at         TEXT NOT NULL,        -- UTC tick timestamp
    station_id         TEXT NOT NULL,
    target_date        TEXT NOT NULL,        -- market local date (ISO YYYY-MM-DD)
    bracket_idx        INTEGER NOT NULL,
    bracket_label      TEXT,
    token_id           TEXT,                 -- YES token
    no_token_id        TEXT,                 -- NO token
    best_ask           REAL,                 -- enriched YES executable ask (buy)
    best_bid           REAL,                 -- enriched NO executable ask (buy)
    yes_top_ask_price  REAL,                 -- top of YES book (cheapest ask)
    yes_top_ask_size   REAL,
    no_top_ask_price   REAL,                 -- top of NO book (cheapest ask)
    no_top_ask_size    REAL,
    volume24hr         REAL
);
CREATE INDEX IF NOT EXISTS idx_book_snapshots_station_date
    ON book_snapshots(station_id, target_date);
CREATE INDEX IF NOT EXISTS idx_book_snapshots_snapped_at
    ON book_snapshots(snapped_at);
