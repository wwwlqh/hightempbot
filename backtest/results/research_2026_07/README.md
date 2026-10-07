# Post-mortem research (2026-07)

Investigations after the first live month ($100 → $154.54, 21 May–13 Jun).
Every avenue below is closed; re-running without new data gives the same
answer.

| Study | Data | Script | Verdict |
|---|---|---|---|
| Neighbor-station ML | `mlds/` | `explore_ml_neighbors.py` | Forecasts −18% MAE, but trading PnL no better than EMOS; neighbors add nothing. |
| All-bracket edge atlas | `atlas/` | `edge_atlas.py` | No tradeable cell outside NO favorites. YES is dead (model 13–36pp overconfident). NO real-fill edge ≈ +2.9% ROS, about half the mid-fill figure. |
| METAR observation sniping | `metar/` | `explore_metar_sniping.py` | Market lags observations ~10 min, but book depth caps profit at ~$1–2k/yr. No-go. |
| TAIL slices | — | `explore_tail_slices.py` | EV ≤ 0 in every slice at real fills. The wait-for-2¢ trigger selected the losers; the +$2,537 backtest was a fill artifact. TAIL disabled 2026-07-17. |
| Calibrated gate | `../sweep_calibrated_gate.csv` | `sweep_calibrated_gate.py` | Basis for the reliability-calibrated NO gate. |

Removed scripts (in git history) found ML did not beat EMOS at real fills,
did not rescue YES/TAIL, and that a 25% drawdown halt would trip ~36% of the
time at 7% sizing.

**Conclusion**: only NO on favorites survives. The limit is capital × book
depth, not the model.
