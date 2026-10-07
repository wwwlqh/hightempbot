---
type: decision
title: "Per-Station PnL Is Noise — Do Not Filter"
created: 2026-05-26
updated: 2026-05-26
decision_date: 2026-05-18
status: implemented
tags: [decision, stations, filtering, bss, pnl]
related:
  - "[[Optimum Strategy]]"
  - "[[Backtest Harness]]"
---

# Per-Station PnL Is Noise — Do Not Filter (2026-05-18)

Backtest split-half analysis on the Candidate #1 80-day window (706 NO+TAIL bets, 29 stations) shows **per-station PnL has no persistent signal**. Filtering live by recent station PnL has no expected benefit. Filtering by BSS is actively harmful.

## Per-station PnL stability

`backtest/analyze_station_pnl_stability.py` ran two split-half tests:

| Test | Spearman correlation |
|---|---|
| Per-station chronological midpoint split (h1_ROI vs h2_ROI) | **+0.024** (≈ zero) |
| Global calendar-midpoint split | **−0.164** (mild *negative*, regression to mean) |

Concrete examples:
- KDAL: −$2.19 h1 → +$1.65 h2 (worst h1 → one of best in h2)
- LTAC: +$3.63 h1 → +$0.52 h2 (best h1 collapsed)
- KMIA: +$1.18 h1 → +$1.08 h2 (steady — but KDAL+LTAC swap outweighs)

## BSS extension — counter-intuitive

Ran the same analysis on per-station Brier Skill Score (`p_E` vs `won_yes`):

| Test | Pearson | Spearman |
|---|---|---|
| BSS_full vs PnL_full (in-sample) | **−0.292** | **−0.292** |
| BSS_h1 vs BSS_h2 (stability) | +0.640 | +0.576 |
| BSS_h1 vs PnL_h2 (walk-forward) | **−0.306** | **−0.267** |
| BSS_h1 vs ROI_h2 (walk-forward) | **−0.412** | −0.259 |

BSS itself **is** stable across halves (Spearman 0.64). But **high-BSS stations have lower PnL**.

Top-BSS, low-PnL: MMMX (BSS 0.71, −$0.40), MPMG (0.63, +$1.11).
Low-BSS, high-PnL: SAEZ (0.20, +$3.81), KMIA (0.25, +$2.27), LFPG (0.23, +$2.92).

## Why

A station with high BSS means EMOS nails its weather. The market *also* nails it (forecasts are public). **No mispricing → no edge.** A station with mid-range BSS has enough difficulty that market opinion diverges from model truth → exploitable.

**Edge comes from market overconfidence, not from raw model skill.**

## How to apply

- **Do not filter live by recent station PnL.** It's noise.
- **Do not raise the `MIN_BSS` floor.** Tightening it removes the highest-PnL stations (SAEZ/KMIA/LFPG, all sub-0.25 BSS).
- **Do not invert the BSS gate** to filter *high*-BSS stations. The sample is too small (29 stations) to act on inversely.
- Keep the current `MIN_BSS` floor — it screens out genuinely useless stations (ZSQD/ZGGG with BSS ~0 that don't generate bets anyway).

**Net rule:** Run all enrolled stations. The strategy IS the strategy.

## Caveats

- Rules out *large* persistent station-edge effects (Spearman > 0.4). A tiny effect (~0.1) could exist but is unmeasurable at 29 stations × ~24 bets each.
- If station-level edge shifts in the future (new ingestion source, model change), rerun `backtest/analyze_station_pnl_stability.py`.

## Where

- Script: `backtest/analyze_station_pnl_stability.py`
- Reuses Candidate #1 wiring from `live_match_eval.candidates_3strats`
- Rerun anytime with fresh `decision_table.parquet`

## Related

- [[invariants#operational]] — don't filter by station PnL or BSS
- [[Optimum Strategy]] — full 46-station enrollment intentional
