---
type: meta
title: "Parity Report: src vs backtest (2026-05-09)"
aliases: ["Parity Report: src vs backtest (2026-05-09)"]
created: 2026-05-09
updated: 2026-05-14
tags: [meta, parity, audit, backtest, calibration]
status: resolved
---

# Parity Report: src vs backtest (2026-05-09)

Full audit of divergence between `src/hightempbot/` live behavior and `backtest/` harness results, conducted 2026-05-09. Two P0 bugs were found and fixed; backtest and live are now at parity on all checked dimensions.

## P0 bugs fixed

### 1. Bracket-bound parsing (98% of PnL shift)

**Bug:** The backtest used "extends-to-next-bracket-edge" semantics — each bracket's bound extended +0.5°F to the adjacent bracket's edge, introducing a systematic +0.5°F bias at every bracket boundary.

**Live behavior:** ROUND-rule midpoints (continuous half-open ranges aligned to the actual market description).

**Fix:** Backtest `parse_bracket_bounds` updated to use ROUND-rule semantics, matching `src/hightempbot/resolution/gamma.py`. Applied in `backtest/lib/live_match_eval.py`.

**Impact:** 98% of the BR100 train PnL shift (bracket parser was the dominant driver, not sigma floor).

See [[2026-05-09 Bracket Parser Parity]] for the full diagnostic and decision.

### 2. EMOS sigma floor (2% of PnL shift)

**Bug:** Backtest used `sigma_floor = 0.5°C`; live uses `sigma_floor = 0.1°C` (5× smaller).

**Fix:** Backtest sigma floor lowered to `0.1°C` to match live. Applied in `backtest/lib/sweep_lib.py`.

**Impact:** 2% of the BR100 train PnL shift. The sigma floor rarely binds on this dataset — most fitted sigmas are well above floor.

## Dimensions checked (no fixes needed)

- LUT bucket grid (`BUCKETS` in live vs `LUT_BUCKETS` in backtest) — identical 8-bucket layout after earlier fix.
- Ensemble member set — `EXPECTED_MODELS` matches between live and backtest.
- Walk-forward leakage gate (`local_date < asof`) — consistent.
- Edge band logic — consistent.

## Post-fix numbers (BR100)

| Window | Pre-fix | Post-fix |
|---|---|---|
| Train PnL | +$124.52 | +$3.46 |
| Test PnL | +$516.55 | +$624.11 |

Train PnL collapsed from +$124 to +$3 — the pre-fix number was inflated by bracket-parser bias. Test PnL improved slightly (+$108) as the corrected harness penalized bets that were profitable only due to the boundary bias.

## See also

[[2026-05-09 Bracket Parser Parity]] · [[Backtest vs Dry-Run Parity Audit]] · [[Backtest Harness]] · [[Optimum Strategy]]
