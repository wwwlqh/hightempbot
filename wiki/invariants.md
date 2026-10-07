---
title: Invariants
type: meta
created: 2026-05-26
updated: 2026-05-31
tags: [invariants, calibration, capital, dashboard, schema]
status: developing
---

# Invariants

Rules that look like style preferences but actually carry correctness. A smart agent will silently violate these unless told. Each rule has a one-line **Why** and a **Where** for the relevant code.

## Calibration

### Ensemble lock — exact membership, not count
The set of Open-Meteo ensemble members at training time MUST equal the set at live scoring time. A 9-of-9 fit and a 9-of-9 live forecast can still mismatch if the *which 9* differs.
- **Why:** EMOS coefficients are fit against the specific joint distribution of the chosen members. Substituting silently invalidates the Gaussian.
- **Where:** `EXPECTED_MODELS` check in [[map|forecast loader]]. The bot must **bail the tick** on mismatch, not trade.

### LUT stale halt
If the walk-forward LUT is more than N days stale for a station, the live bot must halt new placement for that station, not fall back to EMOS-only.
- **Why:** LUT staleness implies the empirical bucket calibration no longer reflects current regime; EMOS-only was tried and reverted (see `decisions/2026-04-27-emos-only-reverted.md`).
- **Where:** [[Walk-forward LUT]] freshness check at tick start.

### Sigma floor
EMOS sigma floor exists; it rarely binds. Do not raise it without evidence. Most fitted sigmas are well above the floor on this dataset.
- **Why:** Parity audit on 2026-05-09 showed sigma floor accounts for ~2% of train PnL shift; bracket parser dominates at 98%. See `decisions/2026-05-09-bracket-parser-parity.md`.

## Trading

### Two-sided edge band
The edge gate is two-sided. NO uses positive edge `+0.03..+0.10`. YES uses **negative** edge `-0.10..-0.03` (contrarian band).
- **Why:** Markets often overprice YES at high `np_p`; a calibrated negative-edge YES is a real signal, not a bug.
- **Where:** edge band logic in `decision/strategies.py`.

### Kelly side formula
Kelly denominator differs by side. YES uses `(1 - price)`. NO uses `price`. Never the same for both.
- **Why:** A flat-formula bug overstates one side by ~5×.
- **Where:** sizing in `decision/`.

### Walk-the-book caps (two-part)
1. **5% depth cap** — max stake at any level = 5% of that level's dollar volume.
2. **Slippage cap** — for strategies without `execution_min_edge`, walker stops at `best_ask + 0.05`. NO and TAIL use the realized-VWAP edge floor instead.
- **Why:** Bounds per-bet slippage and prevents the bot from being a price-taker on thin daily-tmax books.
- **Where:** `execution/walker.py::walk_book_edge_preserving`. See `decisions/Walk-Book Slippage Caps.md`.

### Entry edge and realized edge are different fields
`ledger.edge` is the entry-gate edge at the top-of-book decision price.
`ledger.realized_edge` is the post-fill VWAP edge. Do not overwrite the former
with the latter. The NO/TAIL execution floors are inclusive: NO may fill while
`realized_edge >= 0.05`, and TAIL may fill while `realized_edge >= 0.07`.
- **Why:** NO can enter at >= 9pp edge and legitimately walk down to its
  execution floor. Collapsing those fields makes valid fills look like
  sub-floor entries.
- **Where:** `decision/strategies.py` builds `BetSignal`; `persistence/ledger.py`
  stores `edge` on insert and `realized_edge` from `OrderResult`.

### Volume-None skip
If `volume_usd` is `None`, skip the bet. Never fall back to `MAX_BET_USD`.
- **Why:** A `None` volume means the market is quote-void or freshly listed; defaulting to max-size is the worst possible response.

### Loss confirmation requires executable best_ask
A token is "dead" only when `best_ask <= 0.005` AND that ask is executable. Empty books or zero-bid states are quote voids — route through the Gamma close-state fallback, not the loss path.
- **Where:** `resolution/settler.py`. See loss_confirmation_executable_ask context.

## Sources

### WU is the only resolution source
[[Weather Underground]] `wu_actual` for resolution comes from `PROB_API.actualHighC` only. Never fall back to `polyhightemp` or other sources for true probability calculations.

### Gamma archives close ~24-48h post-close
Polymarket Gamma's `/events?slug=` and `/markets?condition_ids=` return `[]` once daily-tmax events are archived. After this, the bot has no `polymarket_*` resolution path. Fallback is `wu_actual_fallback` via the actuals table (still manual as of 2026-05-26).
- **Where:** `resolution/settler.py`. See [[2026-05-20 Gamma Archives Closed Events]].

## Schema and ledger

### Bet-dict ↔ COLUMNS sync
Adding any field to the bet dict requires adding it to `COLUMNS` in `ledger.py`. The two lists must stay aligned.
- **Why:** Silent column drop. Inserts succeed; the field is lost.

### Row identity via lastrowid
Ledger updates use `cursor.lastrowid`. Never use `ORDER BY ... LIMIT 1` subqueries to locate the just-inserted row.
- **Why:** Race window with concurrent inserts.

### `order_id` UNIQUE partial index
Since 2026-05-13, `ledger` has a partial UNIQUE INDEX on `order_id WHERE order_id IS NOT NULL`. Pre-deploy must run `scripts/check_ledger_order_id_duplicates.py`; `init_db` hard-fails on duplicates.

### Server SQLite is 3.34.1
No `ALTER TABLE ... DROP COLUMN` on the Oracle server. Five dead LUT columns linger in `ledger` until the server SQLite is upgraded.

## Capital

### Exposure cap excludes dry_run
The exposure cap must exclude `dry_run=1` rows. Otherwise dry-run accumulation blocks live trading on day one.

### CANCELLED rows excluded from dashboard counts
CANCELLED bets must not appear in any aggregate dashboard count — wins, losses, PnL, ROI, station tallies.

### Live MAX_DD halt is 0.40
New placement halts at 40% drawdown from peak basis. Lowered from prior 50% on 2026-05-24.

## Retrain and scheduling

### Retrain on resolution, not calendar
Retrain triggers fire after resolutions complete for a city, not on a UTC calendar gate. Each city has its own timezone.

### Horizon bucket timing uses bet timestamp
Time-relative values for LUT bucketing use the bet's placement timestamp, not "today's date" at resolution time.

## Dashboard

### Per-row display stays row-exact
`fill_price`, `fill_size`, `realized_edge`, etc. are per-bet display fields. Never aggregate them across rows. Aggregation is for portfolio panels, not bet-row displays.
- **Why:** Top-up backfills produce multiple ledger rows per logical bet; aggregating obscures the actual fills. See `project_dashboard_separate_workstream`.

### Auth is cookie-based, not Basic
HTMX/XHR do not send Basic Auth headers. The dashboard MUST use cookie auth.

### Tab switching uses inline onclick + htmx.ajax
Do not use `addEventListener` or `fetch` for tab switches; they failed empirically. Pattern: inline `onclick="..."` on a `<div>` calling `htmx.ajax(...)`.

### Bind host explicit
`DASHBOARD_BIND_HOST` env var controls bind host. Empty → legacy fallback (`0.0.0.0` if pass set else loopback). Set explicitly when terminating TLS at a reverse proxy.

## Deployment

### Full src-tree deploy only
Always deploy the entire `src/hightempbot/` tree, not just changed files. Partial `scp` turns refactor-renames into silent runtime crashes.
- **Where:** [CLAUDE.md](../CLAUDE.md) deploy section — rsync preferred, tar-pipe fallback.

### Restart via scripts
Use `restart_bot.sh` on the server (versioned copy: `scripts/restart_bot.sh`; it restarts the bot and its in-process dashboard). Do not kill + start in a compound SSH command.

## Operational

### Station qualification uses MIN(bss)
Qualification is `MIN(bss)` across ALL months, not the current month. A station that was bad once stays disqualified.

### Don't filter live by station PnL or BSS
Per-station PnL is noise (Spearman ~0). BSS is stable but anti-correlated with PnL. See `decisions/2026-05-18-station-pnl-is-noise.md`.

### Pandas datetime alignment
[[Open-Meteo]] ensemble data: Pandas 3 defaults to `datetime64[us]`. Per-hour leakage gates must explicitly cast to `datetime64[s]`. See [[Pandas 3 datetime64 Leakage Bug]].
