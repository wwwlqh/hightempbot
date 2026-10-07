# Backtest harness

Research code for the bot's strategies. Never imported by `hightempbot.*`.

The champion config is `configs/candidate_l2_depth.json`; live
`STRATEGY_CONFIGS` should match it.

**Read `results/research_2026_07/README.md` before trusting any number here.**
The backtests below overfit and did not survive live trading or real-fill
re-scoring.

## Pipeline

```bash
python backtest/scripts/fetch_polymarket_history.py --resume       # prices/metrics -> data/polymarket_history.db
python backtest/scripts/fetch_polymarket_books.py --start <d> --end <d> --missing-only   # L2 books
python backtest/scripts/build_decision_table.py                    # leakage-safe base table
python backtest/scripts/build_l2_decision_table.py                 # + L2 ladders at/before entry
python backtest/scripts/sweep_l2_depth_features.py                 # feature sweep
python backtest/scripts/walkforward_l2_depth_features.py           # strict ABCD selection
```

Set `HTB_TP_SL_CONFIG=/path/to/spec.json` to test another config.

## Layout

| Path | Purpose |
|---|---|
| `lib/sweep_lib.py` | Paths, signal flavors, leakage helpers |
| `lib/live_match_eval.py` | Candidate generator and L2 book-walking execution model |
| `lib/honest_report.py` | Calibration / ROS / °C-vs-°F report |
| `scripts/measure_tp_sl.py` | Simulator and config helpers |
| `scripts/lut_range_chunks.py` | ABCD chunk helpers |
| `scripts/edge_atlas.py`, `scripts/explore_*.py` | Post-mortem research (see research README) |
| `scripts/champion_honest_report.py`, `scripts/shadow_replay.py` | Promotion checks and live parity (see `RUNBOOK_parity.md`) |
| `results/*.csv`, `results/bets/*.parquet` | Sweep outputs and per-bet records |

## Method

Walk-forward over four chunks (A 02-18→03-12, B →04-04, C →04-27,
D →05-21, 2026): train on A, test B; train A+B, test C; train A+B+C, test D.
B–D is one continuous out-of-sample bankroll path.

Leakage rules: EMOS as-of `calibration_params_history`, LUT from
`pred_bucket_history` before the market date, entry ≥ 4h before close.

PMD books exist only for the last 90 days of the plan; L2 coverage was about
61% of entries. Missing ladders fall back to a synthetic liquidity model.

## Historical results (mid-price fills)

| Run | OOS PnL | Max DD | Bets |
|---|---:|---:|---:|
| Strict ABCD selection | +$252 | 18% | 314 |
| TAIL delayed entry (wait for ≤2¢) | +$2,537 | 26% | 308 |
| Sizing NO 7% / TAIL 5%, 100% cap | +$1,345 | 26% | — |

At real L2 fills TAIL was EV ≤ 0 and the NO edge roughly halved
(≈ +2.9% ROS). TAIL was disabled on 2026-07-17.
