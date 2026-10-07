---
title: Polymarket
type: entity
entity_type: prediction_market
created: 2026-06-08
updated: 2026-06-08
status: stable
tags: [entity, polymarket, prediction-market, clob]
aliases: [Polymarket CLOB, Poly]
---

# Polymarket

Decentralized prediction market platform. HighTempBot trades daily-high-temperature binary markets on Polymarket's Central Limit Order Book (CLOB v2).

## Integration

- **Markets:** daily `highest-temperature-in-{city}-on-{month}-{day}-{year}` binary markets with 11 brackets per event.
- **API surfaces used:** Gamma API (`/events`, `/markets`) for market discovery and resolution; CLOB API (`/prices`, `/book`, `/prices-history`) for pricing and order placement; Data API for trade history and open positions.
- **Order types:** FAK BUY (entry), FOK SELL (TP exit via `close_position`).
- **Wallet:** deposit wallet `0xFf4eCB28218af1874da6086d30D1f3125fa1c843` with `POLY_SIGNATURE_TYPE=3` (ERC-1271 deterministic deposit wallet). The legacy proxy wallet is no longer used for order placement.
- **Fees:** `POLY_FEE_THETA = 0.05` (5% of notional).
- **Resolution:** Gamma archives closed events ~24-48h post-close. After archive, `wu_actual_fallback` is the only settlement path. See [[2026-05-20 Gamma Archives Closed Events]].
- **Bracket format:** 11 brackets per market, 2°F steps (US) or 1°C steps (non-US), range shifts daily. See [[invariants#sources]].

## See also

[[HighTempBot Project]] · [[Walk-forward LUT]] · [[2026-05-20 Gamma Archives Closed Events]] · [[Oracle Cloud Server]]
