# Backtest ⇄ live parity

Keep the decision table current so `shadow_replay.py` can compare live bets
with backtest expectations, and promote a champion only on the honest report,
never on PnL alone.

## Refresh (daily, ~24h after markets close)

```bash
python backtest/scripts/fetch_polymarket_history.py --start <lastgood> --end <yesterday> --resume
python backtest/scripts/fetch_polymarket_books.py --start <lastgood> --end <yesterday> --missing-only
python backtest/scripts/build_decision_table.py        # extend END_DATE
python backtest/scripts/build_l2_decision_table.py
python backtest/scripts/champion_honest_report.py
python backtest/scripts/shadow_replay.py --ledger <ledger.db> --bets backtest/results/bets/ \
  --start <-30d> --end <yesterday> --json
```

After fetching, check `fetch_log` has `ok` rows with `n_rows > 0` on both
`prices` and `metrics` for the newest dates. Don't trust shadow replay unless
`MAX(prices.ts_unix)` is within ~48h of now. The PMD price ingest stopped at
2026-05-20, so recent live bets currently match nothing.

## Before promoting a champion

Run `champion_honest_report.py` (and `--immediate-tail`) and require:

1. The fixed A–D total reproduces the committed headline.
2. Per-strategy overconfidence stays in its historical band (NO ≈ +4.9pp).
3. Per-strategy ROS is clearly above breakeven.
4. Both °C and °F are positive on ROS.
5. No high-n reliability bin shows a large adverse gap.
6. Metrics come from walking the real L2 ladder at the intended size. Mid
   fills overstated TAIL (positive → negative) and the champion (~2×).

Once ingest is current, `shadow_replay.py` should show high outcome agreement
and a small, stable `fill_delta_mean`.
