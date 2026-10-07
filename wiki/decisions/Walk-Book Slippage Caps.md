---
type: decision
title: "Walk-Book Slippage Caps"
created: 2026-05-08
updated: 2026-05-31
decision_date: 2026-05-08
status: superseded
tags:
  - decision
  - execution
  - slippage
  - polymarket
  - sizing
related:
  - "Walk-the-book Execution"
  - "Order Execution"
  - "Execution Config"
  - "[[Edge-Preserving Sizing]]"
---

# Walk-Book Slippage Caps

Two-part rule for the live order walker in `src/hightempbot/execution/walker.py` (renamed from `order.py` in U16, 2026-05-19). Bounds how aggressively the bot eats the Polymarket ask book, so per-bet slippage stays under control even when book depth is thin.

## The rule

1. **Per-level depth cap â€” max bet size = 5% of liquidity at best ask.** When the order walker steps into the top-of-book level, it may consume at most 5% of that level's dollar volume. Beyond that, it must walk to the next price level.
2. **Slippage cap â€” VWAP must stay within 0.05 of best ask.** If the walker would have to consume a level whose price exceeds `best_ask + 0.05`, it stops. Whatever has already been filled becomes the executable size; the rest is dropped.

Both caps fire independently. Whichever fires first wins.

## Status

> [!superseded] 2026-06-06 — VWAP cap approach superseded by execution_min_edge floors
> The 0.05 VWAP slippage cap was not independently implemented. Instead, the L2-depth champion (deployed 2026-05-29, updated 2026-06-06) replaced it with per-strategy `execution_min_edge` floors: NO uses `0.05` and TAIL uses `0.07`. Active NO and TAIL pass `max_walk_price=None` — they fill as deep as the realized VWAP edge floor allows. The separate `max_vwap_slip_from_anchor` top-up leash was also removed (2026-05-31). The slippage cap concept is superseded, not completed. See [[Edge-Preserving Sizing#Book Walk]].

| Cap | Status | Where |
|---|---|---|
| **0.05 slippage cap above best ask** | **Superseded** — replaced by per-sleeve `execution_min_edge` floors (NO 0.05, TAIL 0.07). NO and TAIL pass `max_walk_price=None` and rely on realized VWAP edge floor instead. | `src/hightempbot/execution/strategy_constants.py` (`execution_min_edge` field on each `StrategyConfig`) |
| **5%-of-best-ask depth cap** | **Not implemented** — still pending. No code change since 2026-05-08 policy record. | Design sketch in "Implementation sketch" section below |

## Why

Polymarket CLOB depth is shallow on the daily-tmax markets at scanner-time. Without depth caps, a single large fill at the top of the book can:

- Eat the entire visible best-ask layer, exposing the bot as a price-taker
- Move the executable VWAP beyond the edge floor mid-fill (already mitigated by the edge-preserving break inside `walk_book_edge_preserving`)
- Telegraph the bot's presence to other market participants

The 5% cap would force the walker to leave most of the top-of-book depth alone. The 0.05 slippage cap bounds the worst-case execution price only for sleeves that do not use `execution_min_edge`. Active NO/TAIL now use realized VWAP edge floors instead of a fixed price ceiling.

## Backtest correspondence

The backtest walk-book sim uses `EXIT_NO_IMPACT_ZONE = 0.05` â€” the first 5% of liquidity exits at `bid_top` with no impact, beyond that linear impact applies. The 5%-of-best-ask cap on the live side **mirrors** that no-impact zone semantically: live mostly stays inside the no-impact band the backtest assumes.

## Implementation sketch (for the depth cap)

In `walk_book_edge_preserving`, after computing `level_usd = price * size` for each level, cap `take_usd`:

```python
# Per-level depth cap: never take more than 5% of any single level's volume.
# Forces the walker to spread across levels rather than dominating one.
DEPTH_FRACTION_CAP = 0.05
take_usd = min(level_usd * DEPTH_FRACTION_CAP, remaining)
```

Caveats to think through before merging:

- Should the cap apply only to the top level, or to every level the walker consumes? (Top-only is more permissive; uniform is closer to the backtest's exit model.)
- If `target_usd` is small enough that 5% of the top level already covers it, the cap is a no-op â€” fine.
- If depth at the top level is genuinely large (rare on cheap-tail markets, common on NO bets at `npâ‰ˆ0.85`), the 5% cap may be overly restrictive at the top and unnecessarily push the walker into the next level. Consider whether to apply the cap only when `level_usd > target_usd Ã— N` for some N.

## Related invariants

- Walk-the-book Execution â€” sizing from `order_price` (executable limit), not stale `signal.fill_price`. Already enforced.
- [[Edge-Preserving Sizing]] â€” 5% capital fraction per active NO and TAIL slot; pipeline halts new placement at 40% DD. Sets `target_usd` before the walker runs.
- Execution Config â€” per-strategy `max_walk_price=0.05` already set. Adding `max_depth_fraction=0.05` next to it is the natural home for the depth cap.

## See also

Walk-the-book Execution Â· Order Execution Â· Execution Config Â· [[Edge-Preserving Sizing]] Â· Live-Match Evaluator

