---
type: decision
title: "FLIP Sleeve — YES where the NO gate fires"
created: 2026-08-09
updated: 2026-08-09
decision_date: 2026-08-09
status: active
tags: [decision, strategy, flip, yes, experiment, operator-order]
related:
  - "[[Optimum Strategy]]"
  - "[[2026-07-16 Calibrated NO Gate]]"
---

# FLIP Sleeve (2026-08-09)

Operator-ordered live experiment: buy **YES** on exactly the brackets where
the champion **NO** gate fires. Deployed **despite** the −EV evidence — a
replay of the real June+July 2026 live bets at real recorded YES asks scores
**−$89.36**, and EV under the live model is **−73% of stake**. This is a
deliberate operator override, not a model-endorsed strategy.

## Mechanics

- 5th entry in `STRATEGY_CONFIGS` (`"FLIP"`). Enabled ONLY via `FLIP_MODE=1`
  in the **server** `.env`; `_apply_flip_mode()` runs at import and swaps
  `NO.enabled=False` / `FLIP.enabled=True` so the two sleeves can never take
  opposite sides of the same bracket in one tick. Changing the flag requires
  `restart_bot.sh`. (Note 2026-10-07: "server `.env`" / `restart_bot.sh`
  refer to the Oracle deployment, retired 2026-09-11; the bot is undeployed.)
- `_evaluate_flip_branch` re-evaluates the NO gate (calibrated claim, tuned
  band) against the live NO price, then emits a YES signal on that bracket.
- Inverted execution economics: `min_edge=-1.0` is **intentional** (the model
  P(YES) is tiny by construction, so any positive edge floor would gate every
  fill). The ONLY fill boundary is `max_walk_price` — ask book consumed no
  deeper than scanner-time YES ask + 0.05. Do not "fix" the negative edge.
- Sizing `capital_frac=0.07` (same dial as the NO champion), entry hours
  [0..6], holds to resolution.
- Dashboard session reset: `DASHBOARD_SESSION_START_UTC="2026-08-09"` hides
  pre-experiment history from the dashboard (ledger rows untouched).

## Rollback

`FLIP_MODE=0` in the server `.env` + `restart_bot.sh` — the registry reverts
to its committed default (NO on, FLIP off). Fail-closed: any error reading
the config at boot leaves NO on / FLIP off. (Historical: the server `.env`
and `restart_bot.sh` belonged to the Oracle deployment retired 2026-09-11.)

## Sources of truth

- Runtime: `src/hightempbot/execution/strategy_constants.py`
  (`STRATEGY_CONFIGS["FLIP"]`, `_apply_flip_mode`) +
  `runtime_config.Config.flip_mode`
- Gate: `src/hightempbot/decision/strategies.py::_evaluate_flip_branch`
- Evidence: session research 2026-08-09 (flip replay of real fills)

## Related

- [[Optimum Strategy]] — active spec (top note mirrors this decision)
- [[2026-07-16 Calibrated NO Gate]] — the NO gate FLIP mirrors
