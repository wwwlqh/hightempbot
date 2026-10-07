---
title: Glossary
type: meta
created: 2026-05-26
updated: 2026-05-26
tags: [glossary, terms, signals, calibration]
status: developing
---

# Glossary

Domain terms that appear across code, memory, and Polymarket markets. Definitions only. For implementation, follow links into `concepts/` or [[map]].

## Signals (the 9 flavors)

Inputs:
- `E` = `p_emos` — raw EMOS Gaussian `P(temp ∈ bracket)`
- `n_cum` — walk-forward count of prior predictions in this `(station, pred_bucket_low)`
- `hits_cum` — those predictions that hit
- `L_obs` = `hits_cum / max(n_cum, 1)` — empirical bucket hit rate (NaN when `n_cum == 0`)
- `confident` = `n_cum >= 30` — live-bot threshold for "trust the LUT"

| Term | Formula | Use |
|---|---|---|
| **p_E** | `E` | Pure EMOS Gaussian probability |
| **p_L_strict** | `L_obs if confident else NaN` | Pure LUT, skip on cold start |
| **p_L_loose** | `L_obs if confident else E` | LUT with EMOS fallback. **Live default.** |
| **p_B_50** | `0.5·E + 0.5·p_L_loose` | Equal blend |
| **p_Shrink_n10** | shrinkage with prior n=10 | Aggressive empirical lean |
| **p_Shrink_n50** | shrinkage with prior n=50 | Moderate empirical lean |
| ... | ... | See [[Signal Flavors]] for the full 9 |

## Strategies

| Term | Meaning |
|---|---|
| **NO** | Buy NO-side tokens at high `np_p` (model says won't hit). Currently `fp_min=0.75`, `min_edge=0.090`, `max_edge=0.15`, ceiling extension `max_edge=0.35`. |
| **TAIL** | Cheap-tail strategy. Buys YES-side tokens on extreme brackets with consensus vote across signals. `alpha=4.0`, 4-of-4 vote, signal `fp_max=0.03`, delayed-entry trigger `yes_ask <= 0.02`, target size 5% of capital. |
| **YMID** | Mid-bracket YES strategy. **Currently disabled.** |
| **YHIGH** | High-bracket YES strategy. **Currently disabled.** |
| **TAIL consensus skip** | If any bracket in same `(station, target_date)` shows YES ask ≥ 0.40 at tick, TAIL returns no signal. Threshold 0.40 is the L2 champion setting. |

## Brackets

| Term | Meaning |
|---|---|
| **Bracket** | One of 11 temperature ranges per Polymarket daily-tmax market. |
| **°F station** | US stations; bracket width 2°F, total range 18°F. |
| **°C station** | Non-US stations; bracket width 1°C, total range 9°C. |
| **Floor / interior / ceiling** | Bracket structure: 1 floor `(−∞, low)` + 9 interior + 1 ceiling `[high, ∞)`. |
| **Continuous bounds** | DB-stored `bracket_low`/`bracket_high` are TRUE continuous half-open ranges under ROUND semantics. A label "85–90°F" maps to `(84.5, 90.5)`. The 0.5 offset is baked in. |
| **ROUND rule** | Polymarket resolution rule: actual reading rounds to the displayed integer, then matched against the displayed labels. |

## Pricing and execution

| Term | Meaning |
|---|---|
| **np_p** | Normalized price probability — the market's implied `P(side wins)` after de-vigging. |
| **fp_min / fp_max** | Per-strategy fill-price gates. Entry only allowed when token price is within `[fp_min, fp_max]`. |
| **best_ask** | Top of CLOB ask book for the token. |
| **walk-the-book** | Order walker that steps through ask levels accumulating fill until size, depth cap, or slippage cap triggers. |
| **VWAP fill** | Volume-weighted average fill price the walker realizes. |
| **execution_min_edge** | Per-strategy realized-VWAP edge floor. When set (NO, TAIL), replaces the `best_ask + 0.05` slippage stop. |
| **size_frac** | Fraction of capital sized per slot. Current equity-cap deployment: NO `0.07`, TAIL `0.05`; YMID/YHIGH disabled. |

## Calibration

| Term | Meaning |
|---|---|
| **EMOS** | Ensemble Model Output Statistics — Gaussian-on-ensemble calibration. Fits coefficients so the mean & spread of Open-Meteo ensemble members produce a calibrated `N(μ, σ²)` over tmax. |
| **LUT** | Look-Up Table — walk-forward empirical bucket hit rates per `(station, pred_bucket_low)`. |
| **Walk-forward** | Each row uses only data with `local_date < target_date` — strict no-leakage. |
| **Bucket** | A predicted-probability range (e.g. `[0.20, 0.30)`). The LUT tabulates hit-rate per bucket per station. |
| **EXPECTED_MODELS** | The locked set of Open-Meteo ensemble members. Must be identical train vs live. See [[invariants#ensemble-lock]]. |
| **Sigma floor** | Lower bound on EMOS sigma. Rarely binds. |
| **BSS** | Brier Skill Score per station. **Stable across halves (Spearman 0.64) but anti-correlated with PnL (Spearman -0.27).** Don't filter live by BSS. |

## Backtest

| Term | Meaning |
|---|---|
| **BR100** | Backtest run identifier convention — the canonical "100% basis" run used for ABCD comparison. |
| **ABCD chunks** | Four chronological chunks of the validation window. Used to check whether a strategy is positive across all four (robustness) vs. just net-positive overall. |
| **Asia19** | Subset of Asian stations sometimes excluded for analysis. Live code does NOT block Asia19 — exclusion is documentation only. |
| **Candidate #1** | The 2026-05-09 strategy lineage (NO 0.090/0.25 + TAIL α4.5/2%). It is historical rollback context; live uses the L2-depth champion plus NO 7% / TAIL 5% current sizing. |
| **Decision Table** | The cached parquet of `(station, target_date, bracket, signals...)` rows used by the backtest harness. See [[Decision Table]]. |

## Resolution

| Term | Meaning |
|---|---|
| **wu_actual** | Resolution via Weather Underground actual high — `PROB_API.actualHighC`. |
| **wu_actual_fallback** | Manual resolution path used when Polymarket Gamma archives the event. Naming chosen to keep dashboard/backfill behavior intact. |
| **polymarket_gamma_closed** | Resolution via Gamma `closed=true` + `outcomePrices`. Final fallback before WU. |
| **polymarket_clob_terminal** | Resolution via CLOB best-bid/ask reaching ≥ 0.995 or ≤ 0.005. |

## Capital and ledger

| Term | Meaning |
|---|---|
| **stake_basis** | Capital available for sizing. Net of submitted return transfers. |
| **peak_basis** | High-water mark of stake_basis. Used for MAX_DD halt. |
| **MAX_DD** | Maximum drawdown halt threshold. Currently 0.40. |
| **PENDING** | Ledger state for bets where Polymarket has accepted the order but the bot hasn't seen the fill metadata yet. |
| **verification_downgraded** | Flag set when tx hash backfill is needed; no Telegram alert. |
