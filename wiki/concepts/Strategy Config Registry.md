---
title: Strategy Config Registry
type: concept
created: 2026-06-08
updated: 2026-10-07
status: stable
tags: [concept, strategy, configuration, sizing, execution]
aliases: [STRATEGY_CONFIGS, StrategyConfig]
---

# Strategy Config Registry

The `STRATEGY_CONFIGS` dict in `src/hightempbot/execution/strategy_constants.py` is the single source of truth for all per-strategy execution parameters. The corresponding JSON is `backtest/configs/candidate_l2_depth.json` (variant `sel_taila40_fp03_cs40`). `tests/test_strategy_constants.py` fails CI on any drift between the two.

## Sleeves

`STRATEGY_NAMES` registers five sleeves. Committed default: only NO is enabled.

| Strategy | Enabled | Signal | `capital_frac` | `execution_min_edge` | Entry trigger |
|---|---|---|---|---|---|
| **NO** | yes (off when `FLIP_MODE=1`) | `p_E` (strict) / `p_B_50` (ceiling) | 0.07 | 0.05 | none |
| TAIL | no (disabled 2026-07-17: EV<=0 at honest fills) | `tail_vote_avg` (4-of-4) | 0.05 | 0.07 | signal band `yes_ask <= 0.03`; place only when `yes_ask <= 0.02` |
| FLIP | no (on only when `FLIP_MODE=1`) | `p_E_flip` (buys YES where the NO gate fires) | 0.07 | None (`max_walk_price` 0.05 leash) | none |
| YMID | no | `p_Shrink_n50` | - | - | - |
| YHIGH | no | `p_B_50` | - | - | - |

`FLIP_MODE=1` in `.env` swaps the sleeves at boot (`_apply_flip_mode`): NO off,
FLIP on, so the two never take opposite sides of one bracket. See
[[2026-08-09 FLIP Sleeve]].

TAIL's 2c delayed-entry trigger comes from the 2026-06-09 `wait_le_02c` WFO
row in `backtest/results/tail_delayed_entry_wfo.md`: +$2,537.24 B-D
continuous OOS PnL, 26.18% max DD, and +$1,325.80 TAIL PnL on 30 TAIL bets.
Backtest runners that consume `candidate_l2_depth.json` apply
`TAIL.delayed_entry_fp_max`; the dedicated WFO script disables it only when
constructing immediate-entry and threshold comparison variants.

## Key constants

- `MAX_DD = 0.40` - halt threshold; pipeline stops new entries at 40% realized drawdown.
- `MIN_BET_USD = 1.0` - absolute minimum bet size.
- `SCAN_INTERVAL_MINUTES = 10` - betting cycle cadence.
- `LUT_STALE_HOURS = 36` - LUT freshness gate.

## Source Of Truth

1. `backtest/configs/candidate_l2_depth.json` - canonical config.
2. `src/hightempbot/execution/strategy_constants.py::STRATEGY_CONFIGS` - runtime transcription.
3. `tests/test_strategy_constants.py` - CI parity enforcement.

Any live parameter change must update both the JSON and the Python dict; CI catches mismatches.

## See Also

[[Optimum Strategy]] - [[Edge-Preserving Sizing]] - [[Signal Flavors]] - [[Walk-forward LUT]]
