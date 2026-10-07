---
type: decision
title: "Calibrated NO Gate"
created: 2026-08-09
updated: 2026-08-09
decision_date: 2026-07-16
status: active
tags: [decision, strategy, no, calibration, reliability, tail]
related:
  - "[[Optimum Strategy]]"
  - "[[2026-08-09 FLIP Sleeve]]"
  - "[[2026-05-24 Robust NO + Current TAIL]]"
---

# Calibrated NO Gate (2026-07-16)

Live forensics on the 05-21→06-13 run showed the NO sleeve ~6.4pp over-confident
overall (claimed 0.945 vs realized 0.881), concentrated in °C (+10.4pp, ROS
−2.31%). Fix: calibrate the claim, then gate on the calibrated edge.

## What changed

- **Reliability calibration ON** — `RELIABILITY_CALIBRATION_ENABLED=True`. The
  NO gate replaces its raw claimed probability (1 − p_E) with a
  reliability-calibrated value BEFORE the edge/fee computation. Curve type:
  `logit_blend`, fit per bracket-unit group (`NO_C` / `NO_F`) via
  `cli/fit_reliability.py`, loaded from the `reliability_curves` table. With no
  active curve the layer is **identity** — the raw gate over-fires, so curves
  MUST be fitted and committed before any deploy.
- **NO `min_edge` retuned** — 0.090 → 0.04 (2026-07-16 walk-forward
  calibrated-gate sweep, `backtest/scripts/sweep_calibrated_gate.py`: OOS
  n=849, ROS +6.16% at mid fills) → **0.05** on 2026-07-17, after real-fill
  scoring showed the 0.04 gate fires bets the 0.05 walker floor can never
  fill. `max_edge=0.15` unchanged.
- **°C re-admitted behind the curve** — `ALLOWED_BRACKET_UNITS={F,C}`. Behind
  `logit_blend`, °C is the well-calibrated unit; the °F blend is guard-refused
  OOS and runs raw/identity.
- **TAIL disabled 2026-07-17** (with YMID/YHIGH already off) after real-fill
  re-evaluation: TAIL is EV≤0 at honest fills in every tested slice; the
  delayed-entry dump-to-2c trigger IS the adverse selection (dumped tails win
  2.9%, firm-priced tails the rule refuses win 40%); the L2 backtest's TAIL
  profit was a fill artifact. Evidence:
  `backtest/results/research_2026_07/README.md`,
  `backtest/scripts/explore_tail_slices.py`.

## Rollback

Re-enable the flags: set `RELIABILITY_CALIBRATION_ENABLED=False` and/or flip
the sleeve `enabled` fields in
`src/hightempbot/execution/strategy_constants.py::STRATEGY_CONFIGS`
(TAIL/YMID/YHIGH keep their tuned params in the registry), then redeploy +
restart. NO's pre-calibration gate was `min_edge=0.090` raw.

## Sources of truth

- Runtime: `src/hightempbot/execution/strategy_constants.py`
  (`RELIABILITY_CALIBRATION_ENABLED`, `ALLOWED_BRACKET_UNITS`,
  `STRATEGY_CONFIGS["NO"]`)
- Calibration layer: `src/hightempbot/calibration/reliability.py` +
  `cli/fit_reliability.py`

## Related

- [[Optimum Strategy]] — active spec (top note mirrors this decision)
- [[2026-08-09 FLIP Sleeve]] — the experiment layered on top of this gate
