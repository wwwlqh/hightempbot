---
title: Edge-Preserving Sizing
type: concept
aliases: [edge-preserving, walk-book sizing, capital-fraction sizing]
created: 2026-05-04
updated: 2026-06-01
tags: [concept, sizing, edge, gates, kelly]
status: stable
---

# Edge-Preserving Sizing

Bet sizing model introduced **2026-04-24**, replacing the previous Fractional Kelly approach. Kelly fraction is no longer computed. Every passing signal targets `cfg.capital_frac × capital`; the book walker trims to preserve edge; cross-tick top-up (added 2026-05-11) keeps refilling the slot's remainder on later ticks until the entry window closes.

## Sizing Formula

`capital_frac` is the **sole sizing dial** as of 2026-05-11. The legacy `STAKE_CAPS_BY_BANKROLL` ladder was removed in commit `cdb4ed2` — partial fills now top up across ticks rather than being capped at single-tick depth. Per-strategy values come from `STRATEGY_CONFIGS`.

Pseudocode mirrors [strategies.py ~L775-914](../../src/hightempbot/decision/strategies.py):

```python
target_usd      = cfg.capital_frac * capital            # 0.07 NO, 0.050 TAIL, 0.010 YMID/YHIGH
                                                          # pipeline halts at MAX_DD=0.40 before this runs
                                                          # (see "Drawdown Tracking" section). The legacy
                                                          # `dd_reduced` halve-on-DD path was deleted
                                                          # 2026-05-23 — capital_frac is the only dial.

slot_filled, slot_anchor = slot_state(conn, ...)         # cumulative non-cancelled exposure on the slot
remaining       = max(0.0, target_usd - slot_filled)     # top-up remainder
effective_min   = max(MIN_BET_USD, 0.01 * target_usd)    # 1% of target dust floor

# Pre-walk gates
if target_usd < MIN_BET_USD:        skip
if target_usd > capital:            skip                  # never bet more than total capital
if remaining < effective_min:       skip "idempotency"   # slot at/above target

# Walker (sticky anchor: scanner-time top on first fill, slot's first anchor on top-up)
# NO/TAIL use per-sleeve execution_min_edge as the execution floor; strategies
# without that explicit floor still use the sticky price leash.
walked = walk_book_edge_preserving(book, remaining, ...,
                                    walk_anchor_price=entry_top_price)
if walked is None:                  skip "insufficient_depth"

bet_size_usd = walked.filled_usd
if bet_size_usd < MIN_BET_USD:      skip "insufficient_depth"
elif bet_size_usd < effective_min:  skip "insufficient_size"   # post-walk dust guard
```

Audit fields are intentionally split:

- `signal.p_market`, `signal.fill_price`, and `signal.edge` remain the
  entry-gate top-of-book price and entry edge. For NO, this is the value that
  must satisfy `cfg.min_edge` (currently 0.09 on the strict path).
- `signal.limit_price` and `signal.bet_size_usd` come from the decision-time
  walker so the order path knows the deepest acceptable level and trimmed
  notional.
- `ledger.fill_price`, `ledger.fill_size`, `event_detail.fill_levels`, and
  `ledger.realized_edge` are execution results from `OrderResult` after live
  placement or dry-run re-walk.

Do not overwrite the signal entry price/edge with the decision-time VWAP. The
2026-05-26 MPMG audit showed why: a NO entry at `0.87` had about 9.18pp entry
edge, then the walker filled at `0.91` with about 5.34pp realized VWAP edge.
Both were valid under the old 5pp execution floor. Current live floors are
NO >= 5pp and TAIL >= 7pp. Storing only the walked edge makes
the dashboard look like the entry gate traded below the 9pp floor.

Live capital basis update (2026-05-21):

- In live mode, `capital` passed into `_evaluate_strategy` is `stake_basis_capital`, not raw free wallet cash.
- `stake_basis_capital = max(wallet pUSD + CLOB-submitted open PENDING cost basis, initial_bankroll + realized_pnl)`. The old `$0.25` dust tolerance is now a compatibility no-op.
- `deployable_capital` remains wallet cash for affordability and is checked in the pipeline immediately before placement.
- Result: an unrealized first fill does not shrink the target for future top-ups. With BR100 and NO `capital_frac=0.07`, KLAX can show `$4.61 / $7.00 target` even though free wallet cash is `$90.23`.

Key changes vs the pre-2026-05-11 model:

- **No more per-bankroll cap.** `cfg.capital_frac × capital` is what the slot targets, full stop.
- **`remaining` is target − filled.** First fills get the full target (`slot_filled == 0`); top-ups deploy whatever's left.
- **`effective_min` floors per-top-up at 1% of target.** A $5,000 NO target rejects fills below $50 so trickle rows can't accumulate as the slot approaches exhaustion. `MIN_BET_USD = 1.0` is the absolute floor for tiny targets.
- **Sticky anchor.** First fill's scanner-time top price persists into `event_detail.entry_top_price` and is read back on every later tick for audit/sticky slot identity. Active NO/TAIL no longer use it as a first-fill VWAP slip cap; their top-ups recheck current gates and the realized VWAP edge floor.

## Book Walk

`walk_book_edge_preserving` ([walker.py ~L84-230](../../src/hightempbot/execution/walker.py)) walks ask levels from `fill_price` upward, filling until:
1. The target USD (`remaining` for top-ups) is consumed, OR
2. The VWAP of fills would push realized edge below `min_edge`, OR
3. An ask level's price exceeds `walk_anchor + max_walk_price` for strategies that still use the price leash.

Returns `(filled_usd, filled_shares, vwap, limit_price, realized_edge)` or `None` if the book cannot satisfy `MIN_BET_USD` while preserving edge → `gate_results["insufficient_depth"] = False`.

`execution_min_edge` semantics:

- NO keeps the pre-entry additive edge gate at `cfg.min_edge = 0.09`, but walker calls use `execution_min_edge = 0.05`, so the intended target can fill deeper as long as realized VWAP edge stays >= 5pp.
- TAIL keeps its vote/alpha gate, but walker calls use `execution_min_edge = 0.07`; it does not blindly fill toward 100% price.
- When `execution_min_edge` is set, decision-time and order-time walkers pass `max_walk_price=None`, so the old 5-cent leash no longer blocks otherwise-edge-preserving NO/TAIL depth.
- The execution floor is inclusive: realized VWAP edge exactly at the sleeve floor passes;
  the walker stops before the next level only when the resulting edge would be
  below the floor.
- Ledger/dashboard semantics: `edge` is the entry edge; `realized_edge` is the
  walked/fill VWAP edge. A row may therefore have entry edge >= 9pp and
  realized edge near the sleeve floor. That is expected slippage control, not a weakened
  entry gate.

`walk_anchor_price` semantics now diverge by slot state:

- **First fill** (`slot_filled == 0`): anchor = scanner-time top-of-book.
- **Top-up** (`slot_filled > 0`, anchor in event_detail): anchor = the slot's first-fill `entry_top_price`, read back via `slot_state`.
- **Legacy slot** (`slot_filled > 0`, no anchor): the slot is **legacy-locked** — `_evaluate_strategy` skips the bet rather than re-anchor. Fires WARNING + Telegram alert on first occurrence per slot, deduped to DEBUG thereafter.

Post-walk, the strategy-specific edge ceiling is re-checked on `realized_edge` ([strategies.py ~L880-895](../../src/hightempbot/decision/strategies.py)).

## Fill-Price Floor Gates

These prevent placing in calibration-weak zones without using edge math:

| Gate | Config key | Value | Logic |
|------|-----------|-------|-------|
| NO floor | `STRATEGY_CONFIGS["NO"].fp_min` | 0.75 strict / 0.50 ceiling extension | Skip strict NO bets below 0.75; ceiling/high brackets can use the p_B_50 extension down to 0.50. |
| YES floor (historical) | `YES_MIN_FILL_PRICE` (removed 2026-08-09) | 0.35 | Legacy single-gate rule: skipped YES bets where YES ask < 0.35. |
| YES inverse-fill gate | — | — | Only trade YES when NO fill **< 0.65** (asymmetric upside zone). If NO fill unavailable, skip. |

> [!key-insight] Empirical basis for the stricter NO floor
> 23 deep-tail NO bets (fill ≥ 0.70): win rate 83%, PnL +$2.33. 17 mid-fill NO bets (0.50–0.70): win rate 41%, PnL −$1.12. The 2026-05-24 ABCD refresh tightened the active strict floor to 0.75; the old 0.65 global (`NO_MIN_FILL_PRICE`, removed 2026-08-09) is recorded here as history only.

> [!note] Legacy globals removed (2026-08-09)
> `NO_MIN_FILL_PRICE = 0.65`, `YES_MIN_FILL_PRICE = 0.35`, `YES_MIN_EDGE = -0.10`, and `YES_MAX_EDGE = -0.03` were never consulted by the live 4-strategy router (per-strategy `fp_min`/`fp_max` and edge bands in Strategy Config Registry are authoritative). They were deleted from `strategy_constants.py` on 2026-08-09 along with the test pins that asserted them; the values recorded here are the historical record of the old single-gate router's tuning.

## YES Edge Band (Contrarian Zone)

Per-strategy in `STRATEGY_CONFIGS`. The legacy globals `YES_MIN_EDGE = -0.10` and `YES_MAX_EDGE = -0.03` (removed from `strategy_constants.py` 2026-08-09) described the historical band; live YMID uses `alpha_ratio` not an additive band; YHIGH uses additive `min_edge=0.025` / `max_edge=0.30` and only fires on ceiling brackets.

## Edge Ceiling (Anti-Model-Error)

Per-strategy `cfg.max_edge`. Global `MAX_EDGE = 0.10` retained as a defensive global cap; per-strategy ceilings override (NO 0.15 strict / 0.35 ceiling extension, YMID 0.30, YHIGH 0.30 — see Strategy Config Registry). Applied post-walk on realized edge so monotonic walker behavior (VWAP only goes up → edge only goes down) keeps the gate consistent with the pre-walk check.

## Drawdown Tracking

**Operator decision 2026-05-20: drawdown ≥ MAX_DD halts the pipeline** (replaces the prior "halve target_usd" behavior). New bet placement is suspended; existing PENDING positions still resolve normally.

```python
# pipeline.py — fires BEFORE evaluate_station
drawdown = (peak_capital - realized_capital) / peak_capital
if drawdown >= MAX_DD:                    # 0.40
    log_pipeline_health("gates", "SKIP", "Halted: drawdown ...")
    return result                          # no new bets this tick
```

`peak_capital` = historical realized peak (live bets only — dry-run excluded). `realized_capital` = current cost-basis capital excluding unrealized PnL. Both come from `get_capital_snapshot`.

Open positions do not count as drawdown. On 2026-05-21 the bot had `$90.23` wallet cash and `$9.60` open cost basis after three live fills, with no terminal PnL. Dashboard and pipeline DD must read `0.0%`; stale raw wallet peaks such as the `$106.09` pre-migration sample are not drawdown denominators.

The legacy `dd_reduced` halving path was **removed from `_compute_target_usd` on 2026-05-23** ([strategies.py ~L197-202](../../src/hightempbot/decision/strategies.py)). The pipeline halts at `MAX_DD=0.40` before `evaluate_station` is reached, so the in-strategies halve was unreachable. `_compute_target_usd` now returns `cfg.capital_frac * capital` directly; the `peak_capital`/`drawdown_capital` parameters of `evaluate_station` were removed in the same pass. Test sites that previously passed `dd_reduced=False` were updated.

> [!note] Backtest divergence
> The backtest harness (`backtest/lib/live_match_eval.py`) still uses the halve-at-40% semantic. Candidate #1 numbers were tuned under that model. Live now halts at the same threshold — backtest PnL under deep-drawdown scenarios will diverge from live by however many bets the halve path would have continued to place at half size.

## See also

Cross-Tick Top-Up · [[Walk-forward LUT]] · Walk-the-book Execution · Strategy Config Registry · [[WU Consensus Gate]] · Capital Snapshot · [[Optimum Strategy]] · PENDING Ledger Pattern · [[Walk-Book Slippage Caps]]
