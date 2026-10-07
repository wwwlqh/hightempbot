# Research Archive — 2026-07-16/17 Post-Mortem Sweep

Every investigation run after the first live month ($100 → $154.54, 2026-05-21..06-13).
Scripts live in `backtest/scripts/`; data artifacts moved here from session temp dirs
so results are inspectable and regenerable. Verdicts are recorded in the agent memory
index and summarized below — **read the verdicts before re-running anything; every
avenue below is CLOSED and re-running them without new data will reproduce the same
conclusions.**

## Contents

### `mlds/` — Neighbor-ML experiment dataset + outputs
- Inputs: `forecasts.parquet` (71k h1 ensemble rows, 46 stations, Feb–Jul 2026, from
  server forecast_archive), `actuals.parquet` (85k rows to 2021), `neighbors.parquet`
  (8-point ERA5 ring + target, Open-Meteo Archive API), `stations.csv`, `README.md`
  (schemas), `neighbor_gaps.csv`.
- Outputs: `features.parquet` (7636×80 leak-free), `probs_M1/M2/M3_noNbr/M1_D5nbr.parquet`
  (per-model bracket probabilities), `forecast_eval.parquet`, `trade_summary.parquet`.
- Regenerate: `python backtest/scripts/explore_ml_neighbors.py` (supports `--stage
  forecast|trade` cache reuse). Server pulls + Open-Meteo fetch procedure documented in
  `mlds/README.md`.
- **Verdict: forecasts −18% MAE / −16% CRPS (significant) but trading ΔPnL@$10 =
  +$4.74 ± $89 vs EMOS (coin flip). Neighbor ablation ≈ identical → neighbors add
  nothing. Keep EMOS+blend.**

### `atlas/` — Two-sided all-bracket edge atlas
- `edge_atlas_table.csv`, `edge_atlas_cells.csv`: every (side × price-band × unit) cell,
  ABCD walk-forward, real-L2 fills where available, multiplicity-screened.
- Regenerate: `python backtest/scripts/edge_atlas.py`.
- **Verdict: ZERO tradeable cells outside the NO-favorites champion. YES dead everywhere
  (model overconfident 13–36pp exactly where a YES gate would select; spreads 3–6×).
  Champion real-fill reconciliation: pure NO_C calibrated edge ≈ +2.9% ROS at real
  fills (mid-fill +6.16% is flattered ~2×). Expect live ≈ the low number.**

### `metar/` — Observation-sniping feasibility
- `metar_*.csv` (IEM ASOS obs for KLAX/KDAL/KHOU/KMIA/KORD), `metar_vs_wu.csv`
  (97.5% exact °F-max agreement), `events_dead.csv`/`events_lead.csv`/`events_days.csv`/
  `winner_lock.csv` (lock events joined to 10-min PMD prices).
- Regenerate: `python backtest/scripts/explore_metar_sniping.py`.
- **Verdict: market genuinely lags observations (p50 10 min, 0% front-run) but books
  (~$500–970) cap the realistic take to ~$1–2k/yr fleet-wide. Soft NO-GO as a profit
  module; worth only a shadow-log via book_snapshots if ever revisited.**

### TAIL slice hunt (script only, no data dir)
- Regenerate: `python backtest/scripts/explore_tail_slices.py` (reads
  `backtest/data/decision_table_may11plus_l2.parquet` + `polymarket_history.db`).
- **Verdict: TAIL is EV≤0 in every slice at honest fills. The delayed-entry dump-to-2¢
  trigger IS the adverse selection (dumped tails win 2.9%; firm-priced tails the rule
  refuses win 40%, n=5). The old +$2,537 continuous WFO (TAIL +$1,325) was a fill
  artifact — ~0.6% of entries had $10 of real ask at the print; real fills ≈ 4.2¢.
  TAIL disabled in production 2026-07-17.**

### Related (elsewhere)
- Calibrated-gate sweep: `backtest/scripts/sweep_calibrated_gate.py` →
  `backtest/results/sweep_calibrated_gate.csv` (champion amendment evidence).
- Honest reporting + parity: `backtest/lib/honest_report.py`,
  `backtest/scripts/{champion_honest_report,shadow_replay,validate_calibrated_gate}.py`,
  `backtest/RUNBOOK_parity.md`, per-bet records in `backtest/results/bets/`.
- Gap-month forward calibration test (Jun 14–Jul 15): model-level only (°C +0.8pp,
  °F +1.0pp — healthy); trading-level untestable for that window (no prices recorded).
  Live weeks 1–2 with book_snapshots are the first trading-level forward test.

### Removed follow-up scripts (verdicts kept here; code in git history)
- `score_ml_realfill.py` (75ba968): ML vs EMOS re-scored through the real-fill walker —
  no significant difference (all deltas < 0.7 bootstrap SE, n=24–41); ML fires the same
  toxic liquid bets and can't discriminate their losers (Fisher p 0.55–0.76). At
  min_edge 0.05 both turn positive (EMOS +1.16%, M1 +7.85%), validating the gate alignment.
- `score_tail_ml.py` (d2c770b): ML doesn't rescue YES/TAIL — tail AUC 0.815/0.825 vs EMOS
  0.811; 8/9 ML TAIL cells EV<0 at real ~4c fills. Keep TAIL/YES disabled.
- `sim_bankroll_path.py` (d2c770b): ML's 3x single path is one lucky window compounded
  (all rigorous lenses z<0.75); a 25% DD halt has ~36% breach probability at $100/7%
  sizing — keep the deployed 40%.

## Standing conclusion (2026-07-17)
One strategy survives everything: **NO on favorites — °C carries the blend-calibrated
edge, °F rides raw claim, min_edge 0.04, walker floor 0.05.** Model upgrades, YES-side
strategies, and per-station filtering are all closed avenues at current market depth.
The binding constraint is capital × book depth, not intelligence.
