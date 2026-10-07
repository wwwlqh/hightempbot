---
type: decision
title: "Robust NO + Current TAIL"
created: 2026-05-26
updated: 2026-05-29
decision_date: 2026-05-24
status: superseded
tags: [decision, strategy, no, tail, abcd, drawdown]
related:
  - "[[Optimum Strategy]]"
  - "[[2026-05-17 TAIL Consensus Skip 0.50]]"
  - "[[2026-05-18 Station PnL Is Noise]]"
---

# Robust NO + Current TAIL (2026-05-24)

> [!warning] Superseded 2026-05-29 by the L2-depth champion
> This profile was the active production strategy for five days. On 2026-05-29 it was replaced wholesale by the **L2-depth champion** (`backtest/configs/candidate_l2_depth.json`, variant `sel_taila40_fp03_cs40`; commit `b07c13a`), which is selected on real PMD L2 book ladders. The L2 port keeps the NO sleeve's gate but raises NO `execution_min_edge` to 0.05 and retunes TAIL (`alpha` 4.5→4.0, `fp_max` 0.05→0.03, hour {0}→{1}, consensus 0.50→0.40, `execution_min_edge`→0.07), originally added `MAX_L2_ASK_PREMIUM`, and **re-enables TAIL**. On 2026-05-31, operator overrides raised TAIL target size to 5% and removed the temporary NO/TAIL first-fill VWAP top-up slip caps; on 2026-06-06 the ask-premium guard was retired. `MAX_DD=0.40` carries over. See [[Optimum Strategy]] and [[2026-05-17 TAIL Consensus Skip 0.50]]. The content below is rollback/history.

After backfilling PolymarketData/Gamma through 2026-05-21, the ABCD refresh picked **robust NO + current Candidate #1 TAIL** over the prior live NO config. Live `MAX_DD` halt lowered from 0.50 → **0.40**.

## The pick

| Profile | Total | A | B | C | D | Positive chunks | Max DD | Bets |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Prior live (NO+TAIL) | +$394.43 | +$93.46 | **−$0.55** | +$239.36 | +$62.15 | 3/4 | **32.39%** | 685 |
| **Robust NO + current TAIL** | **+$398.95** | +$63.56 | **+$88.48** | +$202.24 | +$44.67 | **4/4** | **21.24%** | **487** |

## Why robust NO wins operationally

- Prior live earned more in A/C/D but **failed chunk B** and carried a 32.39% drawdown.
- Robust NO gives up some upside in favorable chunks, **fixes B by +$89.03**, lowers maxDD by ~11.15pp, and removes 198 lower-quality bets while total PnL rises slightly.
- 4/4 positive chunks vs 3/4. Robustness > peak return.

## Active spec (the deployed config)

```yaml
NO:
  signal: p_E
  signal_for_high_bracket: p_B_50
  no_min_fill_price: 0.75
  no_min_fill_price_for_high: 0.50
  no_min_edge: 0.090
  max_edge: 0.150
  max_edge_for_high: 0.35
  entry_local_hours: [0, 1, 2, 3, 4, 5, 6]
  size_frac: 0.0500
  tp: null
  sl: null

TAIL:  # unchanged from Candidate #1
  vote_signals: [p_E, p_B_50, p_L_loose, p_Shrink_n10]
  alpha: 4.5
  n_required: 4
  fp_min: 0.001
  fp_max: 0.05
  entry_local_hours: [0]
  size_frac: 0.0200
  consensus_skip_threshold: 0.50  # see decisions/2026-05-17

YMID: disabled
YHIGH: disabled
```

`MAX_DD: 0.40` (was 0.50).

## Asia19 note

Excluding the broader Asia19 station set still favors robust NO + current TAIL (+$369.33, 4/4, DD 21.24%, n=456) over prior live (+$329.63, 3/4, DD 34.22%, n=638). **Evidence only — live code does NOT block Asia19 stations.** See [[2026-05-18 Station PnL Is Noise]] for the broader "don't filter stations" rule.

## Sources of truth

- Runtime: `src/hightempbot/execution/strategy_constants.py::STRATEGY_CONFIGS`
- Backtest config: formerly `backtest/configs/candidate1.json` (removed after the L2 champion became the only active config; use git history for this retired spec)
- Detailed evidence: `backtest/results/robust_no_current_tail_2026-05-24.md`

## TAIL lineage

TAIL is **unchanged** from the 2026-05-09 Candidate #1 lineage. That lineage remains rollback context; recover the retired config from git history if needed.

## Related

- [[Optimum Strategy]] — full active-spec page (mirrors this decision)
- [[2026-05-17 TAIL Consensus Skip 0.50]] — operator pick of 0.50 over PnL-optimal 0.40
- [[2026-05-09 Bracket Parser Parity]] — parity baseline the backtest now runs against
