---
type: decision
title: "Polymarket Gamma Archives Closed Daily-Temp Events"
created: 2026-05-26
updated: 2026-05-26
decision_date: 2026-05-20
status: workaround_only
tags: [decision, polymarket, gamma, resolution, archive]
related:
  - "Polymarket Resolution"
  - "[[invariants]]"
---

# Polymarket Gamma Archives Closed Daily-Temp Events (2026-05-20)

Polymarket Gamma drops closed daily-temperature events from `/events?slug=` and `/markets?condition_ids=` ~24–48h post-close. After this, the bot has **no `polymarket_*` resolution path**. Confirmed 2026-05-20 against 9 PENDING dry-run bets across KSEA, LEMD, LFPG, LIMC, LTAC, OEJN×3, OPKC for target_dates 2026-05-17 and 2026-05-18.

## Symptom

`pipeline_health` for stage=`resolution` alternates every tick (10–15 min):
- "No Polymarket market data for {target_date}"
- "No Polymarket terminal price at >= 0.995 or <= 0.005 for {target_date}"

Loops forever until manual intervention.

## Cause

Three Gamma endpoints all return `[]` once the event is archived:
- `/events?slug=highest-temperature-in-{city}-on-{month}-{day}-{year}`
- `/markets?condition_ids={mid}&closed=true`
- `/markets?clob_token_ids={token}&closed=true`

CLOB order books also empty at the same time, so the bot's terminal-price paths AND the existing Gamma close-state fallback both fail.

## Workaround (manual)

Resolve via the `actuals` table (WU). Template at `scripts/manual_resolve_5_17_5_18.py`:
1. Pull `actuals.tmax_celsius`
2. Convert to display unit (°F for US stations, °C otherwise)
3. Round per ROUND rule
4. Call `record_resolution(..., resolution_source='wu_actual_fallback', extra_detail={'fallback_reason': 'polymarket_slug_archived', ...})`

The `wu_actual_fallback` source name is deliberately distinct from `polymarket_*` to keep dashboard/backfill behavior intact — see [[2026-05-09 Bracket Parser Parity]] for why preserving polymarket source labels matters.

## Systemic fix (not yet implemented)

Add a 6th resolution path to `resolution/settler.py` that fires after all five `polymarket_*` paths fail AND `(station, target_date)` is older than ~6h past expected close. Pulls WU actuals (already populated by midnight scrape) and resolves with `wu_actual_fallback`.

Not done yet because:
- Requires careful sequencing — must not preempt a polymarket path that might still resolve.
- "6h past expected close" needs a stable station→close-time map.

## Where

- Resolver: `src/hightempbot/resolution/settler.py`
- Manual template: `scripts/manual_resolve_5_17_5_18.py`
- WU actuals source: `actuals` table

## Related

- Existing Gamma close-state path (depends on slug still being indexed) — see `decisions/Walk-Book Slippage Caps.md` for adjacent settler design
- Quote-void handling: see [[invariants#trading]] on loss confirmation requiring executable best_ask
