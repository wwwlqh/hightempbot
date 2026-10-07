---
type: decision
title: "TAIL consensus_skip_threshold = 0.50"
created: 2026-05-26
updated: 2026-06-03
decision_date: 2026-05-17
status: superseded
tags: [decision, strategy, tail, consensus]
related:
  - "[[Optimum Strategy]]"
  - "[[Signal Flavors]]"
---

# TAIL consensus_skip_threshold = 0.50 (2026-05-17)

> [!warning] Superseded 2026-05-29 — live value is now 0.40
> The L2-depth champion port (commit `b07c13a`) adopts the **PnL-optimal 0.40** wholesale, reversing the operator's 2026-05-17 preference for 0.50 (which had been chosen to preserve dry-run bet flow). At 0.40, TAIL flow is cut ~in half vs 0.50 over the 2026-05-17 eval window. The reasoning below explains why 0.50 was originally picked; the live value is now `STRATEGY_CONFIGS["TAIL"].consensus_skip_threshold = 0.40`. Constants now live in `execution/strategy_constants.py` (renamed from `execution/config.py` in U16). See [[Optimum Strategy]].

TAIL strategy carries `consensus_skip_threshold=0.40` in live config (`src/hightempbot/execution/strategy_constants.py`; was 0.50 from 2026-05-17 to 2026-05-29). When any bracket in the same `(station, target_date)` shows YES `best_ask >=` the threshold at the scanner tick, TAIL silently returns no signal for that tick. This is a station-level market-consensus gate, not a per-candidate LUT `pred_bucket` gate.

## The choice

2D backtest sweep on 2026-05-17 (`backtest/results/consensus_skip_sweep_2d_2026-05-17.md`) found:

| Threshold | TAIL PnL | TAIL bet count | Total PnL |
|---|---|---|---|
| Baseline (None) | +$123 | 116 | — |
| T=0.40 | varied | 49 | **+$555 (max)** |
| **T=0.50** | **+$208 (best TAIL-alone)** | 86 | — |

**Operator picked 0.50 over PnL-optimal 0.40** because dry-run already had too few bets per day. Cutting bet count by ~58% (116 → 49) at T=0.40 would have made live observations too sparse to validate, even though total PnL was higher.

## Why this matters for agents

Do not propose moving TAIL to 0.40 for "max PnL" unless one of these holds:
- Dry-run or live bet count has risen substantially enough to absorb a ~50% cut.
- Live data (not backtest) confirms the 0.40 cells are stable across regimes.

The trade is a known one: sample density vs marginal PnL. Operator preference is **density**.

## NO and YMID/YHIGH

- **NO** has `consensus_skip_threshold=None` deliberately — every NO threshold tested REDUCED NO PnL. NO's `np_p >= 0.70` floor already prices in consensus.
- **YMID** and **YHIGH** are currently disabled; their `consensus_skip_threshold` is also `None` and not relevant.

A candidate's LUT `pred_bucket >= 0.40` is separate from this station-level consensus rule. The active L2 champion does not use a live-only pred-bucket hard skip; a 2026-06-03 rerun found zero current NO/TAIL champion candidates in that bucket.

## Where

- Live spec: `src/hightempbot/execution/strategy_constants.py` -> `STRATEGY_CONFIGS["TAIL"]`
- Sweep results: `backtest/results/consensus_skip_sweep_2d_2026-05-17.md`
- Not in any plan doc — see [[invariants]] note on authoritative sources

## Related

- [[Optimum Strategy]] — active spec including TAIL parameters
- [[Signal Flavors]] — TAIL uses vote consensus across `[p_E, p_B_50, p_L_loose, p_Shrink_n10]`
