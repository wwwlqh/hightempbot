# HighTempBot — codebase guide

## Summary

An autonomous trader for Polymarket's daily "highest temperature in <city>"
markets. Per city it fits EMOS to a 9-model Open-Meteo ensemble, turns the
predictive Gaussian into bracket probabilities, compares them with the live
order book and buys with fee-aware Fill-And-Kill orders when the edge clears a
gate. Settlement follows Polymarket's own close state.

One Python process runs an APScheduler (per-station cron jobs) and a FastAPI
dashboard over one WAL-mode SQLite file. The design rule throughout is
**fail closed**: missing, stale or ambiguous input means no bet. It boots in
dry-run unless every live-readiness check passes.

## End to end

```
main()  config → init_db (refuses duplicate order_ids) → readiness → reconcile (180s cap)
        → dashboard config → register stations → scheduler + dashboard thread
enrollment scan (00:30/06:30/12:30/18:30 UTC)
        new city → enroll_station (8 steps) → hot-add its jobs, no restart
betting tick (every 10 min per station, offset by ICAO)
        cheap gates: operator → target date → entry hour → Open-Meteo readiness
                     → WU source → 95% coverage → LUT age → actuals age → already resolved
        Polymarket market + CLOB book, then forecast ensemble last (exact 9-model set)
        EMOS → bracket probabilities → walk-forward LUT → strategy gates → rank by edge
run_betting_cycle
        operator → capital → wallet → drawdown halt → exposure cap → ensemble → calibration
        per bracket: BEGIN IMMEDIATE re-check → PENDING insert → walk book → FAK order → verify
other jobs: reconcile (5 min), TP/SL (+5 min), WU actuals (00:05 local),
            resolution (after 18:00 local), retrain (daily/monthly), redeem, wallet snapshot
```

**Enrollment** (`enrollment/pipeline.py::enroll_station`, never raises): parse
the resolution source (WU only for live) → geocode via Open-Meteo's geocoding
API → backfill WU actuals → require 95% coverage → backfill forecasts → fit
EMOS → seed LUT → register as DRY_RUN/LIVE.

## Core pieces

- **EMOS** (`calibration/emos.py`): `mu = a + b·mean`,
  `sigma² = exp(c) + exp(d)·var` (population variance, floor 0.1 °C), fitted
  by minimising mean Gaussian CRPS with L-BFGS-B. `P(T > x) = 1 − Φ((x−mu)/sigma)`.
  Ready only with ≥ 30 training pairs.
- **Brackets** (`decision/brackets.py`): label X covers `[X−0.5, X+0.5)`;
  membership is half-open. Any NaN, or a total < 0.95 with a missing end
  bracket, returns no probabilities.
- **LUT** (`calibration/lut.py`): 8 probability buckets with their hit rates,
  counting only days strictly before the market date (matches the backtest's
  `merge_asof(allow_exact_matches=False)`). `lut_bucket_stats` is for the
  dashboard only.
- **Strategies** (`decision/strategies.py`, constants in
  `execution/strategy_constants.py`): NO buys the NO token at 0.75–1.00 when
  `edge = (1−p) − price − fee` is in [0.05, 0.15], with `p`
  reliability-calibrated. FLIP (opt-in) buys YES where NO fires. TAIL,
  YMID, YHIGH are disabled.
- **Sizing**: `capital × capital_frac` (NO 7%). No Kelly. The walker
  (`execution/walker.py::walk_book_edge_preserving`) takes asks cheapest-first
  and stops when realized VWAP edge would drop below 0.05.
- **Risk**: drawdown halt at 40% below the realized-ledger peak (not wallet
  peak); open exposure ≤ 100% of capital, re-checked under `BEGIN IMMEDIATE`
  because sibling station ticks run concurrently.
- **Ledger** (`persistence/ledger.py`): PENDING row is written before the
  order, so a crash leaves a record. Retries re-verify, never re-submit.
  PnL is stored net of the taker fee `0.05·p·(1−p)`.
- **Reconciliation**: matched orphan orders are recovered into the ledger,
  never cancelled; stranded `order_id IS NULL` rows older than 30 min are
  cancelled.
- **Resolution** (`resolution/settler.py`): settle only when every bracket is
  closed and exactly one has YES ≥ 0.995. A loss needs an executable ask
  ≤ 0.005; a missing quote is not a loss. WU fallback is manual only.
- **Dashboard**: cookie auth with rate-limited login; a wallet failure only
  degrades the operator panel; drawdown always uses the fixed session start.

## Likely questions

- **Why SQLite?** Single process, no extra service, ACID. WAL +
  `busy_timeout=5000` handle concurrent threads; it would need Postgres past
  ~50 stations.
- **Why `temperature_2m_previous_day1`?** It is a true day-ahead forecast;
  `temperature_2m_max` mixes in same-day runs and leaks.
- **Why exactly 9 models, not "≥ 9"?** A substituted model passes a count
  check but changes what EMOS was fitted on.
- **Leakage rule?** EMOS params dated ≤ market date (fitted on data to D−1);
  LUT observations strictly < market date; entries ≥ 4h before close.
  `assert_no_leakage` runs before the decision table is written.
- **Why FAK entries, FOK exits, no GTC?** FAK matches the edge-preserving
  walk; exits must be all-or-nothing; resting orders get picked off.
- **Why no Kelly?** Too sensitive to error in `p`; fixed fractions are robust.
- **Why not pick stations by skill?** Per-station BSS is stable but
  anti-correlated with PnL: where the model is good, so is the market.
- **Why trust Polymarket over your WU data for settlement?** Polymarket pays.
- **Why JSON for EMOS params?** Inspectable and safe to load, unlike pickle.

## Bugs worth telling

1. **Too-wide forecast sigma floor** made every bracket look like a NO edge.
   Lesson: a one-sided bet stream means calibration width, not the gates.
2. **pandas 3 datetime64 is microseconds**: `astype("int64") // 10**9` came
   out 1000× too small and the per-hour leakage mask passed everything. Fixed
   with `astype("datetime64[s]")` in both copies of the code.
3. **Bracket parser vs live**: a +0.5 °F bound shift explained ~98% of the
   backtest/live gap; the sigma floor ~2%. Check the parser first.
4. **Kelly denominator** used `1 − price` for NO too, undersizing every NO
   bet. Lesson: implement YES/NO asymmetries as two explicit paths.

## Results

Live trading did not reproduce the backtests: fills the bot could get were
the losing ones (adverse selection). At real L2 fills the NO edge was about
+2.9% ROS and TAIL was negative. See `backtest/results/research_2026_07/`.
