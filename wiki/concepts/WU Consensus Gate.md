---
title: WU Consensus Gate
type: concept
aliases: [wu_consensus, WU gate]
created: 2026-05-04
updated: 2026-05-12
tags: [concept, gate, weather-underground, decision, off]
status: stable
---

# WU Consensus Gate

A pre-placement gate that checks whether [[Weather Underground]]'s live forecast agrees with the bet's side before the order is sent. **Currently OFF** in production (2026-05-11). The implementation is preserved so SHADOW/BLOCK can be re-enabled deliberately alongside a matching backtest gate.

## Operating Modes

Hardcoded in [strategy_constants.py](../../src/hightempbot/execution/strategy_constants.py) (renamed from `execution/config.py` in U16; no env override since 2026-05-10):

| Mode | Behaviour |
|------|-----------|
| `OFF` | Gate is skipped entirely; no WU fetch, no verdict written. **Current production.** |
| `SHADOW` | Verdict written to `gate_results["wu_consensus_shadow"]`; telemetry only. Still fires a per-candidate WU API call. |
| `BLOCK` | Verdict written to `gate_results["wu_consensus"]`; `None` maps to `False` (fail-closed). Blocks the bet. |

> [!key-insight] Why OFF won
> The backtest harness has zero WU consensus logic, so any non-OFF mode is by definition off-spec vs the strategy's tuned numbers (alpha_ratio, capital_frac, edge bands, …). Production was running BLOCK by mistake earlier and silently killed every TAIL bet — TAIL deep-tails the deterministic WU forecast and they naturally disagree. SHADOW still pays the per-candidate WU fetch cost for no gating effect. OFF is the only mode where live matches harness assumptions.

## Verdict Logic

Implemented in `src/hightempbot/decision/strategies.py` → `_gate_wu_consensus()` (moved from `execution/decision.py` in U13, 2026-05-19).

- **YES bet**: WU forecasted tmax must be `clearly_in` the bracket — `lo_eff ≤ wu_max_c < hi_eff`.
- **NO bet**: WU forecasted tmax must be `clearly_out` — `wu_max_c < lo − buffer` OR `wu_max_c ≥ hi + buffer`.
- `WU_CONSENSUS_BUFFER_C = 0.0` (as of 2026-05-04) — literal bracket check, no tolerance slop.

## Fail-Closed Conditions

The gate returns `None` (treated as fail-closed) for:
- Both bracket bounds `None` — degenerate parsing failure.
- Unknown `bracket_unit` (not `"C"` or `"F"`) — fail to avoid 30°C conversion errors.
- WU API unavailable (network / 4xx / 5xx) — `None` propagated, not guessed.

## Efficiency Rules

- Gate only fires for candidates that **would otherwise be placed** — already-failing or idempotency-blocked candidates are short-circuited first. Saves WU API calls and keeps SHADOW telemetry focused.
- Idempotency check runs **before** the WU gate (duplicate bets don't burn an API call).

## Forecast Fetching

`src/hightempbot/ingestion/wu_forecast.py` → `fetch_wu_forecast(icao, target_date)`.

- Hits IBM Weather Company v1 `/forecast/daily/5day.json` (same auth as actuals scraper).
- Returns °C always (requests `units=m`).
- **15-min in-memory TTL cache** keyed on `(icao, target_date_iso)` — multiple candidates in the same scan tick share one scrape.
- Falls back to the daytime daypart `day.temp` when `max_temp` is `None` (overnight forecasts). **Never falls back to `night.temp`** (that's the overnight low, ~10°C below the true daily max).

## Telemetry Fields on BetSignal

| Field | Populated by |
|-------|-------------|
| `wu_forecast_c` | Raw WU tmax in °C at gate time |
| `wu_consensus_verdict` | Raw bool/None verdict regardless of mode |
| `gate_results["wu_consensus_shadow"]` | SHADOW mode only |
| `gate_results["wu_consensus"]` | BLOCK mode only |

## See also

YES+NO Trading · [[Edge-Preserving Sizing]] · [[Walk-forward LUT]] · [[HighTempBot Project]]
