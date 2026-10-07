# RUNBOOK — Backtest ⇄ Live Parity (Phase 2, 2026-07-16)

Keep the decision table current so `shadow_replay.py` can join live bets against
a backtest expectation, and gate every champion promotion on the honest
(calibration / ROS / C-vs-F) report — not on bankroll PnL alone.

---

## 1. The pipeline (what feeds what)

```
polymarketdata.co (PAID)           Gamma API (free)              server LIVE_DB
   │  /prices, /metrics, /books        │  event-slug -> 11 slugs      │  forecast_archive
   ▼                                    ▼                              │  actuals
fetch_polymarket_history.py  ───────────┘                             │  calibration_params_history
   → backtest/data/polymarket_history.db   (markets, prices, metrics) │  pred_bucket_history
fetch_polymarket_books.py                                             │
   → backtest/data/polymarket_books_10m.db (L2 bid/ask ladders)       │
        │                                                             │
        ▼                                                             ▼
build_decision_table.py  ── joins prices + EMOS/LUT (leakage-safe) ───┘
   → backtest/data/decision_table_may11plus.parquet
build_l2_decision_table.py  ── enriches with L2 ladders
   → backtest/data/decision_table_may11plus_l2.parquet
        │
        ▼
champion_honest_report.py  → backtest/results/bets/*.parquet   (per-bet expectation)
shadow_replay.py           → joins those parquet(s) to the live ledger DB
```

The **model side** (LIVE_DB: forecasts, actuals, calibration) is current
(forecast_archive → 2026-05-21, actuals → 2026-05-23). The bottleneck is purely
the **PMD price/book ingest**.

---

## 2. The 2026-05-20 ingest gap (why live can't be joined today)

**Symptom.** `polymarket_history.db` prices end **2026-05-20 02:00 UTC**, metrics
**2026-05-20 10:20 UTC**. The L2 decision table therefore has usable entry
prices only through market_date **2026-05-21** (a 05-21 market's price series was
mid-life at the cutoff). Live trading continued past 05-20, so the two streams
are date-disjoint — `shadow_replay` reports `matched: 0` for any recent window.

**Evidence (from `fetch_log`, last run 2026-05-27T01:10Z):**

| market_date | event (slug) fetches | prices fetches | metrics fetches | price rows |
|---|---:|---:|---:|---:|
| 2026-05-19..20 | 88 | 950 | 950 | 63,074 |
| 2026-05-21 | 45 | 495 | 495 | 49,500 (ends 05-20 02:00) |
| 2026-05-22..26 | 190 | **0** | **0** | **0** |

- Markets (Gamma discovery) exist through market_date **2026-05-26**, but for
  05-22..05-26 **only the event/slug-discovery step ran** — the price+metrics
  fetch step logged nothing and stored nothing.
- The `403` responses (144) are all for market_date **< 05-21** (old backfill
  auth hiccups); there is **no auth failure at the boundary**, so a lapsed key is
  not the proximate cause.

**Most likely cause.** The last ingest (2026-05-27) discovered the newer events
but its price/metrics pull was never advanced past ~05-21 (the `fetch_polymarket_books`
usage note in-repo is literally `--end 2026-05-21`), and PMD's own price series
for those markets had not progressed beyond 2026-05-20 02:00 UTC at fetch time.
Net: the price/book ingest has simply not been moved forward since 2026-05-20.

**Do not start an ingest from this runbook.** Recovery requires a valid
polymarketdata.co key and is an operator action (see §3).

---

## 3. What must run, and how often, to stay joinable

Cadence assumes daily markets that close at local midnight. Run **after** a
market_date has fully closed and PMD has captured its final snapshots (allow
~24h of lag before pulling a date's final series).

| step | command (run from repo root) | cadence |
|---|---|---|
| 1. Prices/metrics ingest | `python backtest/scripts/fetch_polymarket_history.py --start <lastgood> --end <yesterday> --resume` | **daily** |
| 2. L2 books ingest | `python backtest/scripts/fetch_polymarket_books.py --start <lastgood> --end <yesterday> --missing-only` | **daily** |
| 3. Base decision table | `python backtest/scripts/build_decision_table.py` (extend `END_DATE`) | daily / on-demand |
| 4. L2 decision table | `python backtest/scripts/build_l2_decision_table.py` | daily / on-demand |
| 5. Backtest per-bet expectation | `python backtest/scripts/champion_honest_report.py` | after each rebuild |
| 6. Shadow-replay parity | `python backtest/scripts/shadow_replay.py --ledger <ledger.db> --bets backtest/results/bets/ --start <-30d> --end <yesterday> --json` | **nightly** |

**Freshness gate (fail loud):** before trusting a shadow-replay result, assert
`MAX(prices.ts_unix)` in `polymarket_history.db` is within ~48h of "now". Today
it is stuck at 2026-05-20 — so shadow-replay honestly reports the disjoint state
instead of a false parity.

**Ingest health checks after step 1/2:**
- `fetch_log` has `status='ok'` rows with `n_rows>0` for the newest market_dates
  on BOTH the `prices` and `metrics` endpoints (not just `event`).
- No cluster of `403` (auth) or empty `200` (no upstream data) on the newest dates.

---

## 4. Pre-promotion checklist (honest report, not PnL)

Never promote a champion on bankroll PnL alone. PnL hid +4.6pp overconfidence, a
~+5% ROS margin, and a C-vs-F split that later broke live (°C collapsed to
−2.31% ROS while °F earned +8.07%). Before promoting, run:

```
python backtest/scripts/champion_honest_report.py            # as-configured
python backtest/scripts/champion_honest_report.py --immediate-tail   # methodology check
```

and require, from the printed honest report:

1. **Reproducibility.** The fixed A-D total must match the committed headline
   within tolerance. If the script prints
   `** WARNING: fixed PnL differs from the committed headline`, the published
   number is stale — investigate before trusting it. (As of 2026-07-16 it does
   NOT reproduce: current engine yields ~+$1,639 fixed vs the committed +$464.)
2. **Calibration.** `overconfidence_pp` per strategy is disclosed and within the
   historical band (NO ≈ +4.9pp claimed 0.942 / realized 0.892). A sudden jump
   means the signal drifted.
3. **ROS margin.** Per-strategy `ROS%` is positive with margin over breakeven —
   don't ship a sleeve whose ROS is a rounding error even if total PnL is large
   (TAIL can dominate PnL on a handful of bets).
4. **C-vs-F symmetry.** BOTH `by_unit` rows (C and F) must be positive on ROS,
   and `by_strategy_unit` must not hide a sleeve that only works on one unit.
   This is the check that would have caught the live °C collapse.
5. **Reliability table.** No claimed-prob bin should show a large adverse
   `gap_pp` at high n (e.g. the [0.99,1.00] NO bin realizing ≪ 99%).
6. **REAL DEPTH ONLY (operator directive 2026-07-17).** Candidate metrics must
   be computed by walking the real L2 ask ladder for the intended order size;
   mid/print-price numbers are advisory and must be labeled as such. History:
   mid fills flattered TAIL (+$1,325 mirage → negative at real fills; only
   ~0.6% of entries had $10 executable at the print) and the champion (~2×:
   +$522/+6.16% mid vs ~+$39/+2.9% ROS real-fill on the pure-C subset — the
   real-fill number matches live's realized ~2.3pp). Expectation-setting and
   deposit sizing use the real-fill number. See
   `backtest/results/research_2026_07/README.md` and
   `backtest/scripts/edge_atlas.py` for the real-fill evaluation pattern;
   `book_snapshots` (live, 2026-07-16) records per-tick real top-of-book so
   this stays testable forward.

Cross-check the promoted config against live once the ingest is current again:
`shadow_replay.py` must show high outcome agreement and a small, stable
`fill_delta_mean` on matched bets; a widening delta or diverging live-vs-backtest
overconfidence gap is a stop-promotion signal.
