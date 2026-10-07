---
type: decision
title: "Bracket Parser Dominates Backtest Parity"
created: 2026-05-26
updated: 2026-05-26
decision_date: 2026-05-09
status: resolved
tags: [decision, parity, backtest, brackets, calibration]
related:
  - "Bracket Math"
  - "[[Backtest Harness]]"
  - "[[Backtest vs Dry-Run Parity Audit]]"
---

# Bracket Parser Dominates Backtest Parity (2026-05-09)

When backtest results diverge from live, **the bracket-bound parser semantics matter far more than the EMOS sigma floor or other Gaussian parameters** on this dataset.

## Three-way diagnostic

| Configuration | BR100 train PnL |
|---|---|
| Pre-fix `(sigma=0.5, bracket extends-to-next-edge)` | +$124.52 |
| Bracket-only fix `(sigma=0.5, bracket ROUND-rule)` | **+$0.51** (drop of $124) |
| Both fixed `(sigma=0.1 matches live, bracket ROUND-rule)` | +$3.46 (further drop of $3) |

The bracket parser fix accounts for **~98% of the parity shift**. The sigma floor change adds ~2%.

## Why

The +0.5°F bracket-bound shift specifically affected boundary-case resolutions — actuals that fell in the disputed `[X, X+0.5)°F` band changed which bracket they "won." Under the wrong "extends-to-next-edge" rule, an 84.7°F reading on a 85–90 bracket would resolve differently from the correct ROUND-rule which puts 84.5–85 into the lower bracket.

Sigma floor at `0.1°C` rarely binds because most fitted EMOS sigmas are well above 0.1 on this dataset, so changing the floor doesn't move much Gaussian integration mass.

## How to apply

When a backtest result diverges from live performance:

1. **Check `parse_bracket` vs `parse_bracket_bounds` semantics first.** The two parsers consume different inputs (label vs question text) but must produce identical `(lo, hi)` bounds for the same Polymarket bracket.
2. **Do not assume sigma floor or Gaussian params are the cause** of parity issues. Verify with a focused diagnostic (revert one bug at a time) before spending time on calibration tuning.
3. **Train +$3 / test +$624 asymmetry on the post-fix harness is NOT a bug** — it persists under bracket-only fix. The asymmetry is from regime shift + ce-optimize test-aware selection bias, not from code-level parity issues.

## Where

- Live parsers: `src/hightempbot/ingest/` — `parse_bracket`, `parse_bracket_bounds`
- Backtest bracket math: `backtest/` — must call the same parser functions
- ROUND-rule reference: Bracket Math

## Related

- [[Backtest vs Dry-Run Parity Audit]] — the broader parity investigation
- [[invariants#calibration]] — sigma floor is real but minor on this dataset
