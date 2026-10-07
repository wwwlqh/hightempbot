---
title: Capital Snapshot
type: concept
created: 2026-06-08
updated: 2026-06-08
status: stable
tags: [concept, capital, drawdown, execution, sizing]
aliases: [CapitalSnapshot, get_capital_snapshot]
---

# Capital Snapshot

`get_capital_snapshot` in `src/hightempbot/execution/capital.py` returns a `CapitalSnapshot` dataclass used by the betting pipeline for sizing and drawdown gating. Live bets only — dry-run rows are excluded from all calculations.

## Fields

| Field | Description |
|---|---|
| `realized_pnl` | Net PnL from all terminal (WIN/LOSS/PUSH/CLOSED) rows (fees deducted) |
| `pending_exposure` | Sum of cost basis of all PENDING live rows |
| `realized_capital` | `initial_bankroll + realized_pnl` (floor; never goes below bankroll) |
| `deployable_capital` | Free wallet pUSD available for new placement (affordability check) |
| `peak_realized_capital` | Historical peak of `realized_capital` (drawdown denominator) |

## How the pipeline uses it

```python
snapshot = get_capital_snapshot(conn, cfg)
drawdown = (snapshot.peak_realized_capital - snapshot.realized_capital) / snapshot.peak_realized_capital
if drawdown >= MAX_DD:           # halt at 40%
    return empty_cycle_result

target_usd = cfg.capital_frac * snapshot.realized_capital
```

`stake_basis_capital` for sizing is `max(wallet_pusd + open_cost_basis, initial_bankroll + realized_pnl)` — prevents unrealized first fills from shrinking the target for subsequent top-ups.

## Dry-run isolation

Dry-run rows are excluded from `realized_pnl`, `pending_exposure`, and `peak_realized_capital`. This prevents dry-run accumulation from blocking live entries on day one, and prevents dry-run DD from triggering the live halt.

## See also

[[Edge-Preserving Sizing#Drawdown Tracking]] · [[Strategy Config Registry]] · [[Optimum Strategy]]
