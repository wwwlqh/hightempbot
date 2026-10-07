# L2 Sizing Walk-Forward Sweep

Protocol: sizing is selected on prior chunks only. Chunk A is training-only; headline OOS rows run one continuous bankroll path from B through D.
Per-step rows below remain reset-chunk diagnostics used for selection.
Hard training gate: max train DD <= 30.00%.

Decision table: `C:\Users\leowq\OneDrive\Desktop\hightempbot\backtest\data\decision_table_may11plus_l2.parquet`
Config: `C:\Users\leowq\OneDrive\Desktop\hightempbot\backtest\configs\candidate_l2_depth.json`
Starting bankroll: `$100`
Exposure/notional cap basis: `equity`.
Excluded stations: `LTAC, OEJN, OPKC, RJTT, RKPK, RKSI, RPLL, VILK, WIHH, WMKK, WSSS, ZBAA, ZGGG, ZGSZ, ZHHH, ZSPD, ZSQD, ZUCK, ZUUU`
Station universe: 27/46 after exclusion.

## Chunks

- A: 2026-02-18 to 2026-03-12
- B: 2026-03-13 to 2026-04-04
- C: 2026-04-05 to 2026-04-27
- D: 2026-04-28 to 2026-05-21

## Walk-Forward Summary

| selector | continuous B-D PnL | final BR | worst B-D segment | +segments | max DD | bets | NO | TAIL | actual open exp | target cap used | last pre-D size |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| risk_adjusted_dd30 | $+1157.77 | $1257.77 | $+341.63 | 3/3 | 23.68% | 288 | $+664.37 | $+493.40 | 86.3% | 92.2% | NO 5.0%, TAIL 6.0%, cap 100% |
| max_pnl_dd30 | $+1190.45 | $1290.45 | $+310.58 | 3/3 | 28.38% | 286 | $+806.43 | $+384.03 | 90.8% | 97.3% | NO 8.0%, TAIL 6.0%, cap 100% |
| exposure_adjusted_dd30 | $+1239.05 | $1339.05 | $+341.63 | 3/3 | 23.68% | 288 | $+752.58 | $+486.46 | 90.7% | 97.3% | NO 8.0%, TAIL 6.0%, cap 100% |

## Walk-Forward Steps

These step rows show the reset-chunk tests used to choose the next size.

| selector | step | selected size | train pnl | train maxDD | train target cap | test pnl | test DD | test avg stake | actual open exp | target cap used |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| risk_adjusted_dd30 | A->B | NO 5.0%, TAIL 6.0%, cap 100% | $+196.18 | 19.49% | 45.6% | $+442.10 | 23.68% | $11.16 | 57.9% | 63.2% |
| risk_adjusted_dd30 | A+B->C | NO 5.0%, TAIL 6.0%, cap 100% | $+638.29 | 23.68% | 63.2% | $+124.85 | 23.39% | $7.48 | 95.2% | 95.2% |
| risk_adjusted_dd30 | A+B+C->D | NO 5.0%, TAIL 6.0%, cap 100% | $+763.14 | 23.68% | 95.2% | $+68.07 | 14.49% | $7.95 | 69.8% | 69.8% |
| max_pnl_dd30 | A->B | NO 8.0%, TAIL 5.0%, cap 100% | $+199.94 | 29.84% | 66.8% | $+430.85 | 28.38% | $15.60 | 82.1% | 86.3% |
| max_pnl_dd30 | A+B->C | NO 6.0%, TAIL 8.0%, cap 100% | $+705.39 | 28.98% | 80.7% | $+130.04 | 30.87% | $9.38 | 96.8% | 96.8% |
| max_pnl_dd30 | A+B+C->D | NO 8.0%, TAIL 6.0%, cap 100% | $+815.06 | 29.93% | 90.6% | $+93.76 | 19.97% | $13.90 | 94.8% | 94.8% |
| exposure_adjusted_dd30 | A->B | NO 5.0%, TAIL 6.0%, cap 100% | $+196.18 | 19.49% | 45.6% | $+442.10 | 23.68% | $11.16 | 57.9% | 63.2% |
| exposure_adjusted_dd30 | A+B->C | NO 5.0%, TAIL 6.0%, cap 100% | $+638.29 | 23.68% | 63.2% | $+124.85 | 23.39% | $7.48 | 95.2% | 95.2% |
| exposure_adjusted_dd30 | A+B+C->D | NO 8.0%, TAIL 6.0%, cap 100% | $+815.06 | 29.93% | 90.6% | $+93.76 | 19.97% | $13.90 | 94.8% | 94.8% |

## Fixed B-D Continuous Diagnostics

These rows replay one fixed size from B through D without resetting bankroll. They are diagnostics, not the selector source.

| rank | size | B-D PnL | final BR | maxDD | n | fill | actual open exp | target cap used | NO | TAIL |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | NO 8.0%, TAIL 6.0%, cap 100% | $+1496.77 | $1596.77 | 29.93% | 283 | 0.92 | 90.0% | 97.7% | $+1036.50 | $+460.26 |
| 2 | NO 8.0%, TAIL 6.0%, cap 150% | $+1457.39 | $1557.39 | 29.93% | 288 | 0.93 | 132.9% | 143.3% | $+993.64 | $+463.76 |
| 3 | NO 8.0%, TAIL 6.0%, cap 175% | $+1457.39 | $1557.39 | 29.93% | 288 | 0.93 | 132.9% | 143.3% | $+993.64 | $+463.76 |
| 4 | NO 8.0%, TAIL 6.0%, cap 200% | $+1457.39 | $1557.39 | 29.93% | 288 | 0.93 | 132.9% | 143.3% | $+993.64 | $+463.76 |
| 5 | NO 8.0%, TAIL 5.0%, cap 100% | $+1449.12 | $1549.12 | 28.38% | 283 | 0.93 | 91.0% | 97.0% | $+991.44 | $+457.68 |
| 6 | NO 8.0%, TAIL 6.0%, cap 125% | $+1443.77 | $1543.77 | 29.93% | 286 | 0.93 | 108.2% | 118.0% | $+979.00 | $+464.76 |
| 7 | NO 9.0%, TAIL 4.0%, cap 125% | $+1421.92 | $1521.92 | 28.99% | 285 | 0.92 | 112.6% | 118.8% | $+999.92 | $+421.99 |
| 8 | NO 7.0%, TAIL 6.0%, cap 100% | $+1390.75 | $1490.75 | 27.83% | 285 | 0.93 | 89.9% | 98.3% | $+921.24 | $+469.51 |
| 9 | NO 9.0%, TAIL 3.5%, cap 125% | $+1376.74 | $1476.74 | 28.23% | 285 | 0.93 | 112.4% | 117.5% | $+969.10 | $+407.65 |
| 10 | NO 9.0%, TAIL 4.0%, cap 175% | $+1371.14 | $1471.14 | 28.99% | 288 | 0.93 | 149.9% | 156.6% | $+948.24 | $+422.89 |
| 11 | NO 9.0%, TAIL 4.0%, cap 200% | $+1371.14 | $1471.14 | 28.99% | 288 | 0.93 | 149.9% | 156.6% | $+948.24 | $+422.89 |
| 12 | NO 9.0%, TAIL 4.0%, cap 100% | $+1364.57 | $1464.57 | 28.99% | 278 | 0.93 | 99.3% | 99.3% | $+941.86 | $+422.71 |
| 13 | NO 9.0%, TAIL 4.0%, cap 150% | $+1362.93 | $1462.93 | 28.99% | 287 | 0.93 | 142.1% | 148.8% | $+940.10 | $+422.83 |
| 14 | NO 8.0%, TAIL 5.0%, cap 150% | $+1362.82 | $1462.82 | 28.38% | 288 | 0.94 | 134.3% | 141.5% | $+903.90 | $+458.92 |
| 15 | NO 8.0%, TAIL 5.0%, cap 175% | $+1362.82 | $1462.82 | 28.38% | 288 | 0.94 | 134.3% | 141.5% | $+903.90 | $+458.92 |
| 16 | NO 8.0%, TAIL 5.0%, cap 200% | $+1362.82 | $1462.82 | 28.38% | 288 | 0.94 | 134.3% | 141.5% | $+903.90 | $+458.92 |
| 17 | NO 8.0%, TAIL 5.0%, cap 125% | $+1350.23 | $1450.23 | 28.38% | 286 | 0.94 | 109.6% | 116.3% | $+890.90 | $+459.33 |
| 18 | NO 7.0%, TAIL 6.0%, cap 150% | $+1346.23 | $1446.23 | 27.83% | 288 | 0.94 | 117.0% | 126.0% | $+873.26 | $+472.97 |
| 19 | NO 7.0%, TAIL 6.0%, cap 175% | $+1346.23 | $1446.23 | 27.83% | 288 | 0.94 | 117.0% | 126.0% | $+873.26 | $+472.97 |
| 20 | NO 7.0%, TAIL 6.0%, cap 200% | $+1346.23 | $1446.23 | 27.83% | 288 | 0.94 | 117.0% | 126.0% | $+873.26 | $+472.97 |
