# L2 Depth Walk-Forward ABCD

Protocol: expanding walk-forward. Chunk A is training-only; B/C/D are out-of-sample test chunks.

Input sweep: `C:\Users\leowq\OneDrive\Desktop\hightempbot\backtest\results\l2_depth_feature_sweep.csv`

## Summary By Selector

| profile | selector | test total | worst test chunk | +chunks | max test DD | bets | NO | TAIL | final trained variant |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| expanded_all | current | $+123.15 | $+2.61 | 3/3 | 36.62% | 373 | $+73.43 | $+49.72 | sel_taila40_fp03_cs50 |
| expanded_all | risk_adjusted | $+194.05 | $+13.31 | 3/3 | 29.46% | 322 | $+93.95 | $+100.10 | sel_taila40_fp03_cs40 |
| original_178 | current | $+223.17 | $+18.99 | 3/3 | 22.05% | 345 | $+131.04 | $+92.13 | tailh1_p10_noe05_taile07 |
| l2_exec_shape | current | $+223.45 | $+18.99 | 3/3 | 22.05% | 346 | $+131.24 | $+92.22 | tailh1_ref_p09_noe04_taile08 |
| l2_tail_book_no_no_retune (recommended) | risk_adjusted | $+251.96 | $+52.55 | 3/3 | 18.22% | 314 | $+143.02 | $+108.94 | sel_taila40_fp03_cs40 |

## Recommended Profile

`l2_tail_book_no_no_retune` excludes NO-side retunes because the expanded NO grid overfit chunk A and failed the next chunk. It keeps the new L2 book/depth and TAIL refinements, then ranks train candidates by PnL divided by train max drawdown.

Recommended strict OOS: $+251.96, worst chunk $+52.55, max DD 18.22%. Final trained variant for forward use: `sel_taila40_fp03_cs40`.

## Steps

| profile | selector | step | selected variant | train total | train worst | train +chunks | train maxDD | test pnl | test DD | n |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| expanded_all | current | A->B | sel_nofp70_nog09_20 | $+157.65 | $+157.65 | 1/1 | 23.54% | $+2.61 | 36.62% | 125 |
| expanded_all | current | A+B->C | sel_taila40_fp05_cs50 | $+259.99 | $+127.25 | 2/2 | 22.38% | $+67.99 | 27.40% | 164 |
| expanded_all | current | A+B+C->D | sel_taila40_fp03_cs40 | $+411.91 | $+116.26 | 3/3 | 19.49% | $+52.55 | 12.62% | 84 |
| expanded_all | risk_adjusted | A->B | sel_nofp70_nog11_20 | $+147.79 | $+147.79 | 1/1 | 13.52% | $+13.31 | 29.46% | 101 |
| expanded_all | risk_adjusted | A+B->C | sel_taila40_fp03_cs40 | $+283.72 | $+116.26 | 2/2 | 19.49% | $+128.19 | 12.42% | 137 |
| expanded_all | risk_adjusted | A+B+C->D | sel_taila40_fp03_cs40 | $+411.91 | $+116.26 | 3/3 | 19.49% | $+52.55 | 12.62% | 84 |
| original_178 | current | A->B | tailh1_p07_noe05_taile09 | $+121.35 | $+121.35 | 1/1 | 22.67% | $+75.34 | 15.72% | 96 |
| original_178 | current | A+B->C | tail_mid_only | $+177.53 | $+86.88 | 2/2 | 19.48% | $+128.84 | 22.05% | 159 |
| original_178 | current | A+B+C->D | tail_mid_only | $+306.37 | $+86.88 | 3/3 | 22.05% | $+18.99 | 14.45% | 90 |
| l2_exec_shape | current | A->B | tailh1_ref_p07_noe03_taile09 | $+124.32 | $+124.32 | 1/1 | 22.63% | $+75.63 | 15.59% | 97 |
| l2_exec_shape | current | A+B->C | tail_mid_only | $+177.53 | $+86.88 | 2/2 | 19.48% | $+128.84 | 22.05% | 159 |
| l2_exec_shape | current | A+B+C->D | tail_mid_only | $+306.37 | $+86.88 | 3/3 | 22.05% | $+18.99 | 14.45% | 90 |
| l2_tail_book_no_no_retune | risk_adjusted | A->B | sel_bid5c_ge1 | $+121.38 | $+121.38 | 1/1 | 18.49% | $+71.21 | 18.22% | 93 |
| l2_tail_book_no_no_retune | risk_adjusted | A+B->C | sel_taila40_fp03_cs40 | $+283.72 | $+116.26 | 2/2 | 19.49% | $+128.19 | 12.42% | 137 |
| l2_tail_book_no_no_retune | risk_adjusted | A+B+C->D | sel_taila40_fp03_cs40 | $+411.91 | $+116.26 | 3/3 | 19.49% | $+52.55 | 12.62% | 84 |
