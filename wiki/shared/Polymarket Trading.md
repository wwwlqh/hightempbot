---
title: Polymarket Trading
type: concept
created: 2026-06-08
updated: 2026-06-08
status: stable
tags: [concept, polymarket, trading, prediction-market, domain]
---

# Polymarket Trading

Domain overview of trading on Polymarket prediction markets as it applies to HighTempBot.

## Market structure

- **Binary markets:** each market has YES/NO token pairs. HighTempBot primarily trades NO (betting the temperature falls outside a bracket) and occasionally YES via the TAIL strategy.
- **Bracket format:** daily-temperature markets use 11 brackets per event, with 2°F steps for US markets and 1°C steps for non-US markets. Brackets shift daily based on ensemble median.
- **Liquidity:** thin order books on most daily-tmax markets. The walker stays inside the realized VWAP edge floor to avoid price-taking on deep fills.
- **Fees:** 5% of notional (`POLY_FEE_THETA = 0.05`). Fee is deducted at both entry and exit.

## Execution model

HighTempBot uses FAK (Fill-and-Kill) BUY orders for entry and FOK (Fill-or-Kill) SELL orders for TP exits. The walk-the-book executor (`walker.py::walk_book_edge_preserving`) fills up to the realized VWAP edge floor, never beyond. See [[Edge-Preserving Sizing]].

## Resolution

Resolution follows a layered path:
1. `polymarket_winner` → `polymarket_terminal_yes` → `polymarket_terminal_token`
2. `polymarket_gamma_closed` (full-event Gamma close)
3. `polymarket_gamma_closed_bracket` (per-bracket Gamma close)
4. `wu_actual_fallback` (manual, fires after Gamma archives)

Gamma archives events ~24-48h after close, removing them from API responses. See [[2026-05-20 Gamma Archives Closed Events]].

## See also

[[Polymarket]] · [[Edge-Preserving Sizing]] · [[Optimum Strategy]] · [[2026-05-20 Gamma Archives Closed Events]]
