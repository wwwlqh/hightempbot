---
type: decision
title: "EMOS-Only Reverted to EMOS+LUT"
created: 2026-05-26
updated: 2026-05-26
decision_date: 2026-04-27
status: implemented
tags: [decision, strategy, calibration, lut, emos]
related:
  - "[[Walk-forward LUT]]"
  - "[[EMOS Calibration]]"
  - "[[Optimum Strategy]]"
---

# EMOS-Only Reverted to EMOS+LUT (2026-04-27)

On 2026-04-26 the bot was refactored to an **EMOS-only edge-band strategy** — LUT removed, gate cascade reduced to `pass_band + pass_volume + pass_fill_price`, both YES and NO sides eligible under a signed edge band `[-0.10, 0]`. The premise was that LUT compounded EMOS error on 41/48 stations.

**Reverted on 2026-04-27 (commit `655c74b`).** Current strategy is EMOS+LUT point-estimate via `p_L_loose` (LUT with EMOS fallback when `n_cum < 30`).

## Why it was tried
- LUT (Wilson CI from `pred_bucket_history`) appeared to layer a second miscalibration on top of EMOS.
- Live observation (obs 2910): 21% win rate, −$33 PnL vs backtest 64% WR, +13.2% ROI.
- Efficient-market thesis: the market IS the truth signal; EMOS-only with band gate would let the market vote.

## Why it was reverted
The full reasoning lives in the commit and superseded memory entry. Key reasons:
- The 1-week DRY_RUN soak did not validate; backtest-vs-live divergence remained.
- The 11-bracket structure makes YES rarely the favorite, so the signed band was filtering most positive-EV YES bets anyway.
- LUT empirical hit-rate, while imperfect, captures station-specific systematic miscalibration that EMOS Gaussian cannot. The "compounding error" framing was wrong on backtest re-examination.

## What replaced it
The current contract (since 2026-04-27, refined through 2026-05-24):
- `signal: p_L_loose` (LUT when `n_cum ≥ 30`, else EMOS fallback).
- Two-sided edge band: NO `+0.03..+0.10`, YES `-0.10..-0.03` (contrarian, by design).
- `MIN_BUCKET_SAMPLES`, `MIN_EDGE`, `MAX_EDGE` are back.
- LUT-stale halt is live.

See [[Optimum Strategy]] for the active spec.

## Do not redo this
The EMOS-only refactor failed in live deployment. Do not propose removing the LUT again without:
1. A backtest showing EMOS-only beats EMOS+LUT across the full ABCD window.
2. A pre-registered live soak protocol (the 2026-04-26 attempt did not honor its own soak).
3. Concrete evidence that LUT bucket hit rates have stopped tracking station-specific miscalibration.

## Related

- [[invariants#calibration]] — LUT stale halt is a hard invariant
- [[2026-05-09 Bracket Parser Parity]] — parity issues post-revert traced to bracket parser, not LUT
