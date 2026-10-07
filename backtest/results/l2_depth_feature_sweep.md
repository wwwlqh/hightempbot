# L2 Depth Feature Sweep

Decision table: `C:\Users\leowq\OneDrive\Desktop\hightempbot\backtest\data\decision_table_may11plus_l2.parquet`
Config: historical non-L2 starting baseline, formerly `C:\Users\leowq\OneDrive\Desktop\hightempbot\backtest\configs\candidate1.json` (file removed; current sweeps default to `candidate_l2_depth.json`)

## Chunks

- A: 2026-02-18 to 2026-03-12
- B: 2026-03-13 to 2026-04-04
- C: 2026-04-05 to 2026-04-27
- D: 2026-04-28 to 2026-05-21

## Top Variants

| rank | variant | total | worst | +chunks | maxDD | n | NO | TAIL |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | sel_taila40_fp03_cs50 | $+435.60 | $+77.81 | 4/4 | 19.48% | 469 | $+189.06 | $+246.53 |
| 2 | sel_taila40_fp03_cs60 | $+392.46 | $+74.40 | 4/4 | 23.89% | 484 | $+183.96 | $+208.50 |
| 3 | sel_nofp75_nog09_20 | $+380.24 | $+73.26 | 4/4 | 22.29% | 529 | $+198.35 | $+181.89 |
| 4 | sel_nofp75_nog09_12 | $+361.02 | $+72.26 | 4/4 | 23.79% | 401 | $+184.45 | $+176.57 |
| 5 | tailh1_ref_p09_noe04_taile08 | $+359.99 | $+71.53 | 4/4 | 22.64% | 484 | $+181.40 | $+178.59 |
| 6 | tailh1_ref_p08_noe04_taile08 | $+356.62 | $+71.53 | 4/4 | 22.64% | 481 | $+178.19 | $+178.43 |
| 7 | tailh1_ref_p07_noe04_taile08 | $+355.78 | $+71.53 | 4/4 | 22.64% | 480 | $+177.44 | $+178.35 |
| 8 | tailh1_ref_p10_noe04_taile08 | $+350.80 | $+71.53 | 4/4 | 22.64% | 485 | $+171.86 | $+178.94 |
| 9 | tailh1_ref_p12_noe04_taile08 | $+350.80 | $+71.53 | 4/4 | 22.64% | 485 | $+171.86 | $+178.94 |
| 10 | tailh1_ref_p15_noe04_taile08 | $+350.80 | $+71.53 | 4/4 | 22.64% | 485 | $+171.86 | $+178.94 |
| 11 | tailh1_ref_p09_noe05_taile08 | $+350.58 | $+71.53 | 4/4 | 22.67% | 477 | $+172.91 | $+177.67 |
| 12 | tailh1_ref_p10_noe05_taile08 | $+350.58 | $+71.53 | 4/4 | 22.67% | 477 | $+172.91 | $+177.67 |
| 13 | tailh1_ref_p12_noe05_taile08 | $+350.58 | $+71.53 | 4/4 | 22.67% | 477 | $+172.91 | $+177.67 |
| 14 | tailh1_ref_p15_noe05_taile08 | $+350.58 | $+71.53 | 4/4 | 22.67% | 477 | $+172.91 | $+177.67 |
| 15 | tailh1_ref_p08_noe05_taile08 | $+347.30 | $+71.51 | 4/4 | 22.67% | 474 | $+169.77 | $+177.53 |

## All Variants

| variant | total | A | B | C | D | worst | +chunks | maxDD | n | description |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| sel_taila40_fp03_cs50 | $+435.60 | $+105.55 | $+133.89 | $+118.35 | $+77.81 | $+77.81 | 4/4 | 19.48% | 469 | Selected L2 profile with TAIL alpha 4.0, fp_max 3%, consensus skip 50%. |
| sel_taila40_fp03_cs60 | $+392.46 | $+97.59 | $+105.27 | $+115.20 | $+74.40 | $+74.40 | 4/4 | 23.89% | 484 | Selected L2 profile with TAIL alpha 4.0, fp_max 3%, consensus skip 60%. |
| sel_nofp75_nog09_20 | $+380.24 | $+146.45 | $+73.26 | $+80.34 | $+80.19 | $+73.26 | 4/4 | 22.29% | 529 | Selected L2 profile with NO fill >= 75%, NO edge [9%, 20%]. |
| sel_nofp75_nog09_12 | $+361.02 | $+115.62 | $+88.39 | $+84.75 | $+72.26 | $+72.26 | 4/4 | 23.79% | 401 | Selected L2 profile with NO fill >= 75%, NO edge [9%, 12%]. |
| tailh1_ref_p09_noe04_taile08 | $+359.99 | $+120.04 | $+71.53 | $+89.49 | $+78.93 | $+71.53 | 4/4 | 22.64% | 484 | Refined TAIL h1, premium <= 9c, NO execution edge 4%, TAIL execution edge 8%. |
| tailh1_ref_p08_noe04_taile08 | $+356.62 | $+120.04 | $+71.53 | $+89.49 | $+75.56 | $+71.53 | 4/4 | 22.64% | 481 | Refined TAIL h1, premium <= 8c, NO execution edge 4%, TAIL execution edge 8%. |
| tailh1_ref_p07_noe04_taile08 | $+355.78 | $+120.04 | $+71.53 | $+88.65 | $+75.56 | $+71.53 | 4/4 | 22.64% | 480 | Refined TAIL h1, premium <= 7c, NO execution edge 4%, TAIL execution edge 8%. |
| tailh1_ref_p10_noe04_taile08 | $+350.80 | $+120.04 | $+71.53 | $+80.29 | $+78.93 | $+71.53 | 4/4 | 22.64% | 485 | Refined TAIL h1, premium <= 10c, NO execution edge 4%, TAIL execution edge 8%. |
| tailh1_ref_p12_noe04_taile08 | $+350.80 | $+120.04 | $+71.53 | $+80.29 | $+78.93 | $+71.53 | 4/4 | 22.64% | 485 | Refined TAIL h1, premium <= 12c, NO execution edge 4%, TAIL execution edge 8%. |
| tailh1_ref_p15_noe04_taile08 | $+350.80 | $+120.04 | $+71.53 | $+80.29 | $+78.93 | $+71.53 | 4/4 | 22.64% | 485 | Refined TAIL h1, premium <= 15c, NO execution edge 4%, TAIL execution edge 8%. |
| tailh1_ref_p09_noe05_taile08 | $+350.58 | $+117.97 | $+71.53 | $+86.29 | $+74.79 | $+71.53 | 4/4 | 22.67% | 477 | Refined TAIL h1, premium <= 9c, NO execution edge 5%, TAIL execution edge 8%. |
| tailh1_ref_p10_noe05_taile08 | $+350.58 | $+117.97 | $+71.53 | $+86.29 | $+74.79 | $+71.53 | 4/4 | 22.67% | 477 | Refined TAIL h1, premium <= 10c, NO execution edge 5%, TAIL execution edge 8%. |
| tailh1_ref_p12_noe05_taile08 | $+350.58 | $+117.97 | $+71.53 | $+86.29 | $+74.79 | $+71.53 | 4/4 | 22.67% | 477 | Refined TAIL h1, premium <= 12c, NO execution edge 5%, TAIL execution edge 8%. |
| tailh1_ref_p15_noe05_taile08 | $+350.58 | $+117.97 | $+71.53 | $+86.29 | $+74.79 | $+71.53 | 4/4 | 22.67% | 477 | Refined TAIL h1, premium <= 15c, NO execution edge 5%, TAIL execution edge 8%. |
| tailh1_ref_p08_noe05_taile08 | $+347.30 | $+117.97 | $+71.53 | $+86.29 | $+71.51 | $+71.51 | 4/4 | 22.67% | 474 | Refined TAIL h1, premium <= 8c, NO execution edge 5%, TAIL execution edge 8%. |
| tailh1_ref_p07_noe05_taile08 | $+346.51 | $+117.97 | $+71.53 | $+85.50 | $+71.51 | $+71.51 | 4/4 | 22.67% | 473 | Refined TAIL h1, premium <= 7c, NO execution edge 5%, TAIL execution edge 8%. |
| sel_bid5c_ge2 | $+355.13 | $+121.38 | $+71.21 | $+85.33 | $+77.21 | $+71.21 | 4/4 | 18.49% | 448 | Selected L2 profile requiring bid depth >= $2 within displayed-5c. |
| sel_bid5c_ge1 | $+351.49 | $+121.38 | $+71.21 | $+81.69 | $+77.21 | $+71.21 | 4/4 | 18.49% | 449 | Selected L2 profile requiring bid depth >= $1 within displayed-5c. |
| tailh1_ref_p09_noe03_taile08 | $+357.43 | $+120.95 | $+71.81 | $+94.40 | $+70.28 | $+70.28 | 4/4 | 22.63% | 492 | Refined TAIL h1, premium <= 9c, NO execution edge 3%, TAIL execution edge 8%. |
| tailh1_ref_p10_noe03_taile08 | $+347.70 | $+120.95 | $+71.81 | $+84.67 | $+70.28 | $+70.28 | 4/4 | 22.63% | 493 | Refined TAIL h1, premium <= 10c, NO execution edge 3%, TAIL execution edge 8%. |
| tailh1_ref_p12_noe03_taile08 | $+347.70 | $+120.95 | $+71.81 | $+84.67 | $+70.28 | $+70.28 | 4/4 | 22.63% | 493 | Refined TAIL h1, premium <= 12c, NO execution edge 3%, TAIL execution edge 8%. |
| tailh1_ref_p15_noe03_taile08 | $+347.70 | $+120.95 | $+71.81 | $+84.67 | $+70.28 | $+70.28 | 4/4 | 22.63% | 493 | Refined TAIL h1, premium <= 15c, NO execution edge 3%, TAIL execution edge 8%. |
| tailh1_ref_p09_noe06_taile08 | $+357.87 | $+111.95 | $+68.35 | $+94.87 | $+82.71 | $+68.35 | 4/4 | 22.76% | 465 | Refined TAIL h1, premium <= 9c, NO execution edge 6%, TAIL execution edge 8%. |
| tailh1_ref_p10_noe06_taile08 | $+357.87 | $+111.95 | $+68.35 | $+94.87 | $+82.71 | $+68.35 | 4/4 | 22.76% | 465 | Refined TAIL h1, premium <= 10c, NO execution edge 6%, TAIL execution edge 8%. |
| tailh1_ref_p12_noe06_taile08 | $+357.87 | $+111.95 | $+68.35 | $+94.87 | $+82.71 | $+68.35 | 4/4 | 22.76% | 465 | Refined TAIL h1, premium <= 12c, NO execution edge 6%, TAIL execution edge 8%. |
| tailh1_ref_p15_noe06_taile08 | $+357.87 | $+111.95 | $+68.35 | $+94.87 | $+82.71 | $+68.35 | 4/4 | 22.76% | 465 | Refined TAIL h1, premium <= 15c, NO execution edge 6%, TAIL execution edge 8%. |
| tailh1_ref_p08_noe06_taile08 | $+356.27 | $+111.95 | $+68.35 | $+94.87 | $+81.10 | $+68.35 | 4/4 | 22.76% | 464 | Refined TAIL h1, premium <= 8c, NO execution edge 6%, TAIL execution edge 8%. |
| tailh1_ref_p07_noe06_taile08 | $+355.41 | $+111.95 | $+68.35 | $+94.01 | $+81.10 | $+68.35 | 4/4 | 22.76% | 463 | Refined TAIL h1, premium <= 7c, NO execution edge 6%, TAIL execution edge 8%. |
| tailh1_ref_p09_noe03_taile07 | $+349.77 | $+120.95 | $+68.06 | $+90.48 | $+70.28 | $+68.06 | 4/4 | 22.63% | 494 | Refined TAIL h1, premium <= 9c, NO execution edge 3%, TAIL execution edge 7%. |
| tailh1_ref_p10_noe03_taile07 | $+340.43 | $+120.95 | $+68.06 | $+81.15 | $+70.28 | $+68.06 | 4/4 | 22.63% | 495 | Refined TAIL h1, premium <= 10c, NO execution edge 3%, TAIL execution edge 7%. |
| tailh1_ref_p12_noe03_taile07 | $+340.43 | $+120.95 | $+68.06 | $+81.15 | $+70.28 | $+68.06 | 4/4 | 22.63% | 495 | Refined TAIL h1, premium <= 12c, NO execution edge 3%, TAIL execution edge 7%. |
| tailh1_ref_p15_noe03_taile07 | $+340.43 | $+120.95 | $+68.06 | $+81.15 | $+70.28 | $+68.06 | 4/4 | 22.63% | 495 | Refined TAIL h1, premium <= 15c, NO execution edge 3%, TAIL execution edge 7%. |
| sel_bid5c_ge5 | $+346.38 | $+119.37 | $+67.93 | $+83.59 | $+75.49 | $+67.93 | 4/4 | 18.52% | 438 | Selected L2 profile requiring bid depth >= $5 within displayed-5c. |
| sel_taila40_fp05_cs50 | $+395.81 | $+127.25 | $+132.74 | $+67.99 | $+67.83 | $+67.83 | 4/4 | 27.40% | 501 | Selected L2 profile with TAIL alpha 4.0, fp_max 5%, consensus skip 50%. |
| sel_tail_bid5c_ge5 | $+356.99 | $+117.97 | $+67.79 | $+92.97 | $+78.26 | $+67.79 | 4/4 | 22.67% | 475 | Selected L2 profile requiring TAIL bid depth >= $5 within displayed-5c. |
| tailh1_ref_p09_noe04_taile07 | $+352.58 | $+120.04 | $+67.79 | $+85.82 | $+78.93 | $+67.79 | 4/4 | 22.64% | 486 | Refined TAIL h1, premium <= 9c, NO execution edge 4%, TAIL execution edge 7%. |
| sel_tail_bid5c_ge2 | $+350.30 | $+117.97 | $+67.79 | $+86.29 | $+78.26 | $+67.79 | 4/4 | 22.67% | 477 | Selected L2 profile requiring TAIL bid depth >= $2 within displayed-5c. |
| tailh1_ref_p08_noe04_taile07 | $+349.21 | $+120.04 | $+67.79 | $+85.82 | $+75.56 | $+67.79 | 4/4 | 22.64% | 483 | Refined TAIL h1, premium <= 8c, NO execution edge 4%, TAIL execution edge 7%. |
| tailh1_ref_p07_noe04_taile07 | $+348.42 | $+120.04 | $+67.79 | $+85.03 | $+75.56 | $+67.79 | 4/4 | 22.64% | 482 | Refined TAIL h1, premium <= 7c, NO execution edge 4%, TAIL execution edge 7%. |
| sel_tail_bid5c_ge1 | $+346.79 | $+117.97 | $+67.79 | $+82.77 | $+78.26 | $+67.79 | 4/4 | 22.67% | 478 | Selected L2 profile requiring TAIL bid depth >= $1 within displayed-5c. |
| tailh1_ref_p10_noe04_taile07 | $+343.87 | $+120.04 | $+67.79 | $+77.10 | $+78.93 | $+67.79 | 4/4 | 22.64% | 487 | Refined TAIL h1, premium <= 10c, NO execution edge 4%, TAIL execution edge 7%. |
| tailh1_ref_p12_noe04_taile07 | $+343.87 | $+120.04 | $+67.79 | $+77.10 | $+78.93 | $+67.79 | 4/4 | 22.64% | 487 | Refined TAIL h1, premium <= 12c, NO execution edge 4%, TAIL execution edge 7%. |
| tailh1_ref_p15_noe04_taile07 | $+343.87 | $+120.04 | $+67.79 | $+77.10 | $+78.93 | $+67.79 | 4/4 | 22.64% | 487 | Refined TAIL h1, premium <= 15c, NO execution edge 4%, TAIL execution edge 7%. |
| tailh1_p10_noe05_taile07 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | TAIL h1, premium <= 10c, NO execution edge 5%, TAIL execution edge 7%. |
| tailh1_ref_p09_noe05_taile07 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Refined TAIL h1, premium <= 9c, NO execution edge 5%, TAIL execution edge 7%. |
| tailh1_ref_p10_noe05_taile07 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Refined TAIL h1, premium <= 10c, NO execution edge 5%, TAIL execution edge 7%. |
| tailh1_ref_p12_noe05_taile07 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Refined TAIL h1, premium <= 12c, NO execution edge 5%, TAIL execution edge 7%. |
| tailh1_ref_p15_noe05_taile07 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Refined TAIL h1, premium <= 15c, NO execution edge 5%, TAIL execution edge 7%. |
| sel_tail_h1 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with TAIL local hours (1,). |
| sel_no_h0_6 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with NO local hours (0, 1, 2, 3, 4, 5, 6). |
| sel_nop10_tailp03 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with NO premium <= 10c and TAIL premium <= 3c. |
| sel_nop10_tailp05 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with NO premium <= 10c and TAIL premium <= 5c. |
| sel_nop10_tailp07 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with NO premium <= 10c and TAIL premium <= 7c. |
| sel_nop10_tailp10 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with NO premium <= 10c and TAIL premium <= 10c. |
| sel_nop10_tailp12 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with NO premium <= 10c and TAIL premium <= 12c. |
| sel_nop12_tailp03 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with NO premium <= 12c and TAIL premium <= 3c. |
| sel_nop12_tailp05 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with NO premium <= 12c and TAIL premium <= 5c. |
| sel_nop12_tailp07 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with NO premium <= 12c and TAIL premium <= 7c. |
| sel_nop12_tailp10 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with NO premium <= 12c and TAIL premium <= 10c. |
| sel_nop12_tailp12 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with NO premium <= 12c and TAIL premium <= 12c. |
| sel_nofp75_nog09_15 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with NO fill >= 75%, NO edge [9%, 15%]. |
| sel_taila45_fp05_cs50 | $+343.32 | $+117.97 | $+67.79 | $+82.77 | $+74.79 | $+67.79 | 4/4 | 22.67% | 479 | Selected L2 profile with TAIL alpha 4.5, fp_max 5%, consensus skip 50%. |
| tailh1_ref_p08_noe05_taile07 | $+340.04 | $+117.97 | $+67.79 | $+82.77 | $+71.51 | $+67.79 | 4/4 | 22.67% | 476 | Refined TAIL h1, premium <= 8c, NO execution edge 5%, TAIL execution edge 7%. |
| tailh1_p07_noe05_taile07 | $+339.28 | $+117.97 | $+67.79 | $+82.01 | $+71.51 | $+67.79 | 4/4 | 22.67% | 475 | TAIL h1, premium <= 7c, NO execution edge 5%, TAIL execution edge 7%. |
| tailh1_ref_p07_noe05_taile07 | $+339.28 | $+117.97 | $+67.79 | $+82.01 | $+71.51 | $+67.79 | 4/4 | 22.67% | 475 | Refined TAIL h1, premium <= 7c, NO execution edge 5%, TAIL execution edge 7%. |
| sel_nop07_tailp03 | $+339.28 | $+117.97 | $+67.79 | $+82.01 | $+71.51 | $+67.79 | 4/4 | 22.67% | 475 | Selected L2 profile with NO premium <= 7c and TAIL premium <= 3c. |
| sel_nop07_tailp05 | $+339.28 | $+117.97 | $+67.79 | $+82.01 | $+71.51 | $+67.79 | 4/4 | 22.67% | 475 | Selected L2 profile with NO premium <= 7c and TAIL premium <= 5c. |
| sel_nop07_tailp07 | $+339.28 | $+117.97 | $+67.79 | $+82.01 | $+71.51 | $+67.79 | 4/4 | 22.67% | 475 | Selected L2 profile with NO premium <= 7c and TAIL premium <= 7c. |
| sel_nop07_tailp10 | $+339.28 | $+117.97 | $+67.79 | $+82.01 | $+71.51 | $+67.79 | 4/4 | 22.67% | 475 | Selected L2 profile with NO premium <= 7c and TAIL premium <= 10c. |
| sel_nop07_tailp12 | $+339.28 | $+117.97 | $+67.79 | $+82.01 | $+71.51 | $+67.79 | 4/4 | 22.67% | 475 | Selected L2 profile with NO premium <= 7c and TAIL premium <= 12c. |
| tailh1_ref_p08_noe03_taile08 | $+354.22 | $+120.95 | $+71.81 | $+94.40 | $+67.07 | $+67.07 | 4/4 | 22.63% | 489 | Refined TAIL h1, premium <= 8c, NO execution edge 3%, TAIL execution edge 8%. |
| tailh1_ref_p08_noe03_taile07 | $+346.56 | $+120.95 | $+68.06 | $+90.48 | $+67.07 | $+67.07 | 4/4 | 22.63% | 491 | Refined TAIL h1, premium <= 8c, NO execution edge 3%, TAIL execution edge 7%. |
| tailh1_ref_p07_noe03_taile08 | $+352.64 | $+120.95 | $+71.81 | $+93.15 | $+66.73 | $+66.73 | 4/4 | 22.63% | 486 | Refined TAIL h1, premium <= 7c, NO execution edge 3%, TAIL execution edge 8%. |
| tailh1_ref_p07_noe03_taile07 | $+345.03 | $+120.95 | $+68.06 | $+89.29 | $+66.73 | $+66.73 | 4/4 | 22.63% | 488 | Refined TAIL h1, premium <= 7c, NO execution edge 3%, TAIL execution edge 7%. |
| sel_nofp80_nog07_12 | $+276.08 | $+71.18 | $+66.07 | $+73.21 | $+65.62 | $+65.62 | 4/4 | 23.97% | 457 | Selected L2 profile with NO fill >= 80%, NO edge [7%, 12%]. |
| tailh1_p06_noe05_taile07 | $+344.37 | $+116.50 | $+65.20 | $+91.17 | $+71.51 | $+65.20 | 4/4 | 22.69% | 471 | TAIL h1, premium <= 6c, NO execution edge 5%, TAIL execution edge 7%. |
| sel_spread_le_10c | $+350.87 | $+116.50 | $+65.18 | $+98.38 | $+70.81 | $+65.18 | 4/4 | 22.69% | 467 | Selected L2 profile requiring L2 spread <= 10c when bid+ask exist. |
| sel_no_h1_6 | $+320.48 | $+114.12 | $+64.84 | $+67.10 | $+74.41 | $+64.84 | 4/4 | 19.56% | 455 | Selected L2 profile with NO local hours (1, 2, 3, 4, 5, 6). |
| sel_taila45_fp03_cs50 | $+328.93 | $+85.80 | $+64.80 | $+93.64 | $+84.69 | $+64.80 | 4/4 | 20.60% | 461 | Selected L2 profile with TAIL alpha 4.5, fp_max 3%, consensus skip 50%. |
| tailh1_ref_p09_noe06_taile07 | $+350.36 | $+111.95 | $+64.68 | $+91.03 | $+82.71 | $+64.68 | 4/4 | 22.76% | 467 | Refined TAIL h1, premium <= 9c, NO execution edge 6%, TAIL execution edge 7%. |
| tailh1_ref_p10_noe06_taile07 | $+350.36 | $+111.95 | $+64.68 | $+91.03 | $+82.71 | $+64.68 | 4/4 | 22.76% | 467 | Refined TAIL h1, premium <= 10c, NO execution edge 6%, TAIL execution edge 7%. |
| tailh1_ref_p12_noe06_taile07 | $+350.36 | $+111.95 | $+64.68 | $+91.03 | $+82.71 | $+64.68 | 4/4 | 22.76% | 467 | Refined TAIL h1, premium <= 12c, NO execution edge 6%, TAIL execution edge 7%. |
| tailh1_ref_p15_noe06_taile07 | $+350.36 | $+111.95 | $+64.68 | $+91.03 | $+82.71 | $+64.68 | 4/4 | 22.76% | 467 | Refined TAIL h1, premium <= 15c, NO execution edge 6%, TAIL execution edge 7%. |
| tailh1_ref_p08_noe06_taile07 | $+348.75 | $+111.95 | $+64.68 | $+91.03 | $+81.10 | $+64.68 | 4/4 | 22.76% | 466 | Refined TAIL h1, premium <= 8c, NO execution edge 6%, TAIL execution edge 7%. |
| tailh1_ref_p07_noe06_taile07 | $+347.91 | $+111.95 | $+64.68 | $+90.18 | $+81.10 | $+64.68 | 4/4 | 22.76% | 465 | Refined TAIL h1, premium <= 7c, NO execution edge 6%, TAIL execution edge 7%. |
| sel_taila40_fp07_cs50 | $+362.65 | $+107.52 | $+122.80 | $+64.50 | $+67.83 | $+64.50 | 4/4 | 28.92% | 509 | Selected L2 profile with TAIL alpha 4.0, fp_max 7%, consensus skip 50%. |
| tailh1_ref_p09_noe03_taile06 | $+375.33 | $+120.95 | $+64.42 | $+122.89 | $+67.07 | $+64.42 | 4/4 | 22.63% | 499 | Refined TAIL h1, premium <= 9c, NO execution edge 3%, TAIL execution edge 6%. |
| tailh1_ref_p10_noe03_taile06 | $+364.06 | $+120.95 | $+64.42 | $+111.63 | $+67.07 | $+64.42 | 4/4 | 22.63% | 500 | Refined TAIL h1, premium <= 10c, NO execution edge 3%, TAIL execution edge 6%. |
| tailh1_ref_p12_noe03_taile06 | $+364.06 | $+120.95 | $+64.42 | $+111.63 | $+67.07 | $+64.42 | 4/4 | 22.63% | 500 | Refined TAIL h1, premium <= 12c, NO execution edge 3%, TAIL execution edge 6%. |
| tailh1_ref_p15_noe03_taile06 | $+364.06 | $+120.95 | $+64.42 | $+111.63 | $+67.07 | $+64.42 | 4/4 | 22.63% | 500 | Refined TAIL h1, premium <= 15c, NO execution edge 3%, TAIL execution edge 6%. |
| tailh1_ref_p09_noe04_taile06 | $+377.66 | $+120.04 | $+64.15 | $+117.91 | $+75.56 | $+64.15 | 4/4 | 22.64% | 491 | Refined TAIL h1, premium <= 9c, NO execution edge 4%, TAIL execution edge 6%. |
| tailh1_ref_p08_noe04_taile06 | $+374.38 | $+120.04 | $+64.15 | $+117.91 | $+72.28 | $+64.15 | 4/4 | 22.64% | 488 | Refined TAIL h1, premium <= 8c, NO execution edge 4%, TAIL execution edge 6%. |
| tailh1_ref_p07_noe04_taile06 | $+373.51 | $+120.04 | $+64.15 | $+117.04 | $+72.28 | $+64.15 | 4/4 | 22.64% | 487 | Refined TAIL h1, premium <= 7c, NO execution edge 4%, TAIL execution edge 6%. |
| tailh1_ref_p09_noe05_taile06 | $+368.32 | $+117.97 | $+64.15 | $+114.68 | $+71.52 | $+64.15 | 4/4 | 22.67% | 484 | Refined TAIL h1, premium <= 9c, NO execution edge 5%, TAIL execution edge 6%. |
| tailh1_ref_p10_noe05_taile06 | $+368.32 | $+117.97 | $+64.15 | $+114.68 | $+71.52 | $+64.15 | 4/4 | 22.67% | 484 | Refined TAIL h1, premium <= 10c, NO execution edge 5%, TAIL execution edge 6%. |
| tailh1_ref_p12_noe05_taile06 | $+368.32 | $+117.97 | $+64.15 | $+114.68 | $+71.52 | $+64.15 | 4/4 | 22.67% | 484 | Refined TAIL h1, premium <= 12c, NO execution edge 5%, TAIL execution edge 6%. |
| tailh1_ref_p15_noe05_taile06 | $+368.32 | $+117.97 | $+64.15 | $+114.68 | $+71.52 | $+64.15 | 4/4 | 22.67% | 484 | Refined TAIL h1, premium <= 15c, NO execution edge 5%, TAIL execution edge 6%. |
| tailh1_ref_p10_noe04_taile06 | $+366.64 | $+120.04 | $+64.15 | $+106.89 | $+75.56 | $+64.15 | 4/4 | 22.64% | 492 | Refined TAIL h1, premium <= 10c, NO execution edge 4%, TAIL execution edge 6%. |
| tailh1_ref_p12_noe04_taile06 | $+366.64 | $+120.04 | $+64.15 | $+106.89 | $+75.56 | $+64.15 | 4/4 | 22.64% | 492 | Refined TAIL h1, premium <= 12c, NO execution edge 4%, TAIL execution edge 6%. |
| tailh1_ref_p15_noe04_taile06 | $+366.64 | $+120.04 | $+64.15 | $+106.89 | $+75.56 | $+64.15 | 4/4 | 22.64% | 492 | Refined TAIL h1, premium <= 15c, NO execution edge 4%, TAIL execution edge 6%. |
| tailh1_ref_p08_noe05_taile06 | $+365.13 | $+117.97 | $+64.15 | $+114.68 | $+68.33 | $+64.15 | 4/4 | 22.67% | 481 | Refined TAIL h1, premium <= 8c, NO execution edge 5%, TAIL execution edge 6%. |
| tailh1_ref_p07_noe05_taile06 | $+364.27 | $+117.97 | $+64.15 | $+113.83 | $+68.33 | $+64.15 | 4/4 | 22.67% | 480 | Refined TAIL h1, premium <= 7c, NO execution edge 5%, TAIL execution edge 6%. |
| sel_taila45_fp07_cs50 | $+317.78 | $+99.46 | $+64.14 | $+79.40 | $+74.79 | $+64.14 | 4/4 | 22.81% | 486 | Selected L2 profile with TAIL alpha 4.5, fp_max 7%, consensus skip 50%. |
| tailh1_ref_p08_noe03_taile06 | $+372.21 | $+120.95 | $+64.42 | $+122.89 | $+63.95 | $+63.95 | 4/4 | 22.63% | 496 | Refined TAIL h1, premium <= 8c, NO execution edge 3%, TAIL execution edge 6%. |
| tailh1_ref_p07_noe03_taile06 | $+370.60 | $+120.95 | $+64.42 | $+121.62 | $+63.62 | $+63.62 | 4/4 | 22.63% | 493 | Refined TAIL h1, premium <= 7c, NO execution edge 3%, TAIL execution edge 6%. |
| sel_nofp75_nog07_20 | $+326.15 | $+117.81 | $+74.80 | $+70.61 | $+62.94 | $+62.94 | 4/4 | 24.78% | 648 | Selected L2 profile with NO fill >= 75%, NO edge [7%, 20%]. |
| tailh1_p05_noe05_taile07 | $+331.75 | $+112.11 | $+62.11 | $+88.93 | $+68.60 | $+62.11 | 4/4 | 22.72% | 464 | TAIL h1, premium <= 5c, NO execution edge 5%, TAIL execution edge 7%. |
| sel_nop05_tailp03 | $+331.75 | $+112.11 | $+62.11 | $+88.93 | $+68.60 | $+62.11 | 4/4 | 22.72% | 464 | Selected L2 profile with NO premium <= 5c and TAIL premium <= 3c. |
| sel_nop05_tailp05 | $+331.75 | $+112.11 | $+62.11 | $+88.93 | $+68.60 | $+62.11 | 4/4 | 22.72% | 464 | Selected L2 profile with NO premium <= 5c and TAIL premium <= 5c. |
| sel_nop05_tailp07 | $+331.75 | $+112.11 | $+62.11 | $+88.93 | $+68.60 | $+62.11 | 4/4 | 22.72% | 464 | Selected L2 profile with NO premium <= 5c and TAIL premium <= 7c. |
| sel_nop05_tailp10 | $+331.75 | $+112.11 | $+62.11 | $+88.93 | $+68.60 | $+62.11 | 4/4 | 22.72% | 464 | Selected L2 profile with NO premium <= 5c and TAIL premium <= 10c. |
| sel_nop05_tailp12 | $+331.75 | $+112.11 | $+62.11 | $+88.93 | $+68.60 | $+62.11 | 4/4 | 22.72% | 464 | Selected L2 profile with NO premium <= 5c and TAIL premium <= 12c. |
| sel_taila40_fp05_cs60 | $+349.50 | $+119.31 | $+104.30 | $+61.40 | $+64.49 | $+61.40 | 4/4 | 27.64% | 517 | Selected L2 profile with TAIL alpha 4.0, fp_max 5%, consensus skip 60%. |
| sel_no_h0_8 | $+352.83 | $+127.74 | $+61.32 | $+81.61 | $+82.16 | $+61.32 | 4/4 | 26.03% | 572 | Selected L2 profile with NO local hours (0, 1, 2, 3, 4, 5, 6, 7, 8). |
| tailh1_ref_p09_noe06_taile06 | $+376.27 | $+111.95 | $+61.10 | $+123.92 | $+79.30 | $+61.10 | 4/4 | 22.76% | 472 | Refined TAIL h1, premium <= 9c, NO execution edge 6%, TAIL execution edge 6%. |
| tailh1_ref_p10_noe06_taile06 | $+376.27 | $+111.95 | $+61.10 | $+123.92 | $+79.30 | $+61.10 | 4/4 | 22.76% | 472 | Refined TAIL h1, premium <= 10c, NO execution edge 6%, TAIL execution edge 6%. |
| tailh1_ref_p12_noe06_taile06 | $+376.27 | $+111.95 | $+61.10 | $+123.92 | $+79.30 | $+61.10 | 4/4 | 22.76% | 472 | Refined TAIL h1, premium <= 12c, NO execution edge 6%, TAIL execution edge 6%. |
| tailh1_ref_p15_noe06_taile06 | $+376.27 | $+111.95 | $+61.10 | $+123.92 | $+79.30 | $+61.10 | 4/4 | 22.76% | 472 | Refined TAIL h1, premium <= 15c, NO execution edge 6%, TAIL execution edge 6%. |
| tailh1_ref_p08_noe06_taile06 | $+374.72 | $+111.95 | $+61.10 | $+123.92 | $+77.75 | $+61.10 | 4/4 | 22.76% | 471 | Refined TAIL h1, premium <= 8c, NO execution edge 6%, TAIL execution edge 6%. |
| tailh1_ref_p07_noe06_taile06 | $+373.83 | $+111.95 | $+61.10 | $+123.03 | $+77.75 | $+61.10 | 4/4 | 22.76% | 470 | Refined TAIL h1, premium <= 7c, NO execution edge 6%, TAIL execution edge 6%. |
| tailh1_p03_noe05_taile07 | $+324.65 | $+100.82 | $+60.95 | $+77.57 | $+85.30 | $+60.95 | 4/4 | 22.82% | 417 | TAIL h1, premium <= 3c, NO execution edge 5%, TAIL execution edge 7%. |
| sel_nop03_tailp03 | $+324.65 | $+100.82 | $+60.95 | $+77.57 | $+85.30 | $+60.95 | 4/4 | 22.82% | 417 | Selected L2 profile with NO premium <= 3c and TAIL premium <= 3c. |
| sel_nop03_tailp05 | $+324.65 | $+100.82 | $+60.95 | $+77.57 | $+85.30 | $+60.95 | 4/4 | 22.82% | 417 | Selected L2 profile with NO premium <= 3c and TAIL premium <= 5c. |
| sel_nop03_tailp07 | $+324.65 | $+100.82 | $+60.95 | $+77.57 | $+85.30 | $+60.95 | 4/4 | 22.82% | 417 | Selected L2 profile with NO premium <= 3c and TAIL premium <= 7c. |
| sel_nop03_tailp10 | $+324.65 | $+100.82 | $+60.95 | $+77.57 | $+85.30 | $+60.95 | 4/4 | 22.82% | 417 | Selected L2 profile with NO premium <= 3c and TAIL premium <= 10c. |
| sel_nop03_tailp12 | $+324.65 | $+100.82 | $+60.95 | $+77.57 | $+85.30 | $+60.95 | 4/4 | 22.82% | 417 | Selected L2 profile with NO premium <= 3c and TAIL premium <= 12c. |
| tailh1_p03_noe07_taile07 | $+309.19 | $+91.11 | $+60.95 | $+74.48 | $+82.65 | $+60.95 | 4/4 | 22.82% | 406 | TAIL h1, premium <= 3c, NO execution edge 7%, TAIL execution edge 7%. |
| sel_spread_le_08c | $+333.75 | $+114.46 | $+60.66 | $+96.05 | $+62.59 | $+60.66 | 4/4 | 22.72% | 455 | Selected L2 profile requiring L2 spread <= 8c when bid+ask exist. |
| sel_spread_le_05c | $+319.43 | $+102.38 | $+59.63 | $+67.19 | $+90.22 | $+59.63 | 4/4 | 24.75% | 415 | Selected L2 profile requiring L2 spread <= 5c when bid+ask exist. |
| tailh1_p06_noe07_taile07 | $+325.80 | $+103.52 | $+58.03 | $+85.19 | $+79.05 | $+58.03 | 4/4 | 22.82% | 446 | TAIL h1, premium <= 6c, NO execution edge 7%, TAIL execution edge 7%. |
| tailh1_p07_noe07_taile07 | $+325.80 | $+103.52 | $+58.03 | $+85.19 | $+79.05 | $+58.03 | 4/4 | 22.82% | 446 | TAIL h1, premium <= 7c, NO execution edge 7%, TAIL execution edge 7%. |
| tailh1_p10_noe07_taile07 | $+325.80 | $+103.52 | $+58.03 | $+85.19 | $+79.05 | $+58.03 | 4/4 | 22.82% | 446 | TAIL h1, premium <= 10c, NO execution edge 7%, TAIL execution edge 7%. |
| sel_nofp75_nog07_15 | $+296.90 | $+95.88 | $+69.29 | $+73.82 | $+57.91 | $+57.91 | 4/4 | 23.56% | 601 | Selected L2 profile with NO fill >= 75%, NO edge [7%, 15%]. |
| sel_nofp75_nog07_12 | $+300.08 | $+98.23 | $+81.53 | $+62.91 | $+57.41 | $+57.41 | 4/4 | 23.98% | 535 | Selected L2 profile with NO fill >= 75%, NO edge [7%, 12%]. |
| sel_nofp80_nog07_20 | $+298.03 | $+81.42 | $+57.07 | $+76.99 | $+82.56 | $+57.07 | 4/4 | 22.50% | 504 | Selected L2 profile with NO fill >= 80%, NO edge [7%, 20%]. |
| tailh1_p5_e7_enable_yhigh | $+315.86 | $+101.59 | $+56.64 | $+80.40 | $+77.24 | $+56.64 | 4/4 | 22.79% | 459 | TAIL h1 + 5c premium + 7pp edge + re-enable YHIGH. |
| tailh1_p04_noe05_taile07 | $+331.03 | $+109.22 | $+56.61 | $+83.22 | $+81.98 | $+56.61 | 4/4 | 22.79% | 448 | TAIL h1, premium <= 4c, NO execution edge 5%, TAIL execution edge 7%. |
| tailh1_p5_e7_fill100 | $+318.49 | $+101.22 | $+56.12 | $+80.56 | $+80.58 | $+56.12 | 4/4 | 22.82% | 438 | TAIL h1 + 5c premium + 7pp edge + require full target fill. |
| premium5_exec7_tail_h1 | $+316.45 | $+101.22 | $+56.12 | $+83.12 | $+75.99 | $+56.12 | 4/4 | 22.82% | 441 | 5c premium + 7pp execution edge, TAIL local hour 1. |
| tailh1_p05_noe07_taile07 | $+316.45 | $+101.22 | $+56.12 | $+83.12 | $+75.99 | $+56.12 | 4/4 | 22.82% | 441 | TAIL h1, premium <= 5c, NO execution edge 7%, TAIL execution edge 7%. |
| sel_taila50_fp03_cs40 | $+350.55 | $+97.09 | $+91.29 | $+106.37 | $+55.80 | $+55.80 | 4/4 | 19.48% | 433 | Selected L2 profile with TAIL alpha 5.0, fp_max 3%, consensus skip 40%. |
| sel_taila45_fp03_cs40 | $+343.52 | $+93.71 | $+91.29 | $+102.71 | $+55.80 | $+55.80 | 4/4 | 19.48% | 435 | Selected L2 profile with TAIL alpha 4.5, fp_max 3%, consensus skip 40%. |
| sel_nofp80_nog07_15 | $+280.72 | $+73.23 | $+55.60 | $+81.04 | $+70.86 | $+55.60 | 4/4 | 22.69% | 492 | Selected L2 profile with NO fill >= 80%, NO edge [7%, 15%]. |
| tailh1_p5_e7_depth5c_ge2 | $+319.28 | $+101.22 | $+55.36 | $+86.71 | $+75.99 | $+55.36 | 4/4 | 23.16% | 439 | TAIL h1 + 5c premium + 7pp edge + $2 depth within 5c. |
| sel_nofp80_nog09_12 | $+294.00 | $+90.31 | $+55.16 | $+84.39 | $+64.15 | $+55.16 | 4/4 | 21.18% | 330 | Selected L2 profile with NO fill >= 80%, NO edge [9%, 12%]. |
| tailh1_p04_noe07_taile07 | $+309.99 | $+98.55 | $+54.66 | $+80.10 | $+76.68 | $+54.66 | 4/4 | 22.82% | 432 | TAIL h1, premium <= 4c, NO execution edge 7%, TAIL execution edge 7%. |
| sel_taila40_fp07_cs60 | $+304.99 | $+90.35 | $+95.52 | $+54.63 | $+64.49 | $+54.63 | 4/4 | 30.64% | 528 | Selected L2 profile with TAIL alpha 4.0, fp_max 7%, consensus skip 60%. |
| tailh1_ref_p09_noe03_taile05 | $+361.91 | $+120.95 | $+53.99 | $+119.90 | $+67.07 | $+53.99 | 4/4 | 25.64% | 503 | Refined TAIL h1, premium <= 9c, NO execution edge 3%, TAIL execution edge 5%. |
| tailh1_ref_p08_noe03_taile05 | $+358.79 | $+120.95 | $+53.99 | $+119.90 | $+63.95 | $+53.99 | 4/4 | 25.64% | 500 | Refined TAIL h1, premium <= 8c, NO execution edge 3%, TAIL execution edge 5%. |
| tailh1_ref_p07_noe03_taile05 | $+357.18 | $+120.95 | $+53.99 | $+118.63 | $+63.62 | $+53.99 | 4/4 | 25.64% | 497 | Refined TAIL h1, premium <= 7c, NO execution edge 3%, TAIL execution edge 5%. |
| tailh1_ref_p10_noe03_taile05 | $+350.64 | $+120.95 | $+53.99 | $+108.64 | $+67.07 | $+53.99 | 4/4 | 25.64% | 504 | Refined TAIL h1, premium <= 10c, NO execution edge 3%, TAIL execution edge 5%. |
| tailh1_ref_p12_noe03_taile05 | $+350.64 | $+120.95 | $+53.99 | $+108.64 | $+67.07 | $+53.99 | 4/4 | 25.64% | 504 | Refined TAIL h1, premium <= 12c, NO execution edge 3%, TAIL execution edge 5%. |
| tailh1_ref_p15_noe03_taile05 | $+350.64 | $+120.95 | $+53.99 | $+108.64 | $+67.07 | $+53.99 | 4/4 | 25.64% | 504 | Refined TAIL h1, premium <= 15c, NO execution edge 3%, TAIL execution edge 5%. |
| tailh1_ref_p09_noe04_taile05 | $+364.26 | $+120.04 | $+53.74 | $+114.92 | $+75.56 | $+53.74 | 4/4 | 25.75% | 495 | Refined TAIL h1, premium <= 9c, NO execution edge 4%, TAIL execution edge 5%. |
| tailh1_ref_p08_noe04_taile05 | $+360.98 | $+120.04 | $+53.74 | $+114.92 | $+72.28 | $+53.74 | 4/4 | 25.75% | 492 | Refined TAIL h1, premium <= 8c, NO execution edge 4%, TAIL execution edge 5%. |
| tailh1_ref_p07_noe04_taile05 | $+360.11 | $+120.04 | $+53.74 | $+114.05 | $+72.28 | $+53.74 | 4/4 | 25.75% | 491 | Refined TAIL h1, premium <= 7c, NO execution edge 4%, TAIL execution edge 5%. |
| tailh1_p10_noe05_taile05 | $+354.93 | $+117.97 | $+53.74 | $+111.69 | $+71.52 | $+53.74 | 4/4 | 25.75% | 488 | TAIL h1, premium <= 10c, NO execution edge 5%, TAIL execution edge 5%. |
| tailh1_ref_p09_noe05_taile05 | $+354.93 | $+117.97 | $+53.74 | $+111.69 | $+71.52 | $+53.74 | 4/4 | 25.75% | 488 | Refined TAIL h1, premium <= 9c, NO execution edge 5%, TAIL execution edge 5%. |
| tailh1_ref_p10_noe05_taile05 | $+354.93 | $+117.97 | $+53.74 | $+111.69 | $+71.52 | $+53.74 | 4/4 | 25.75% | 488 | Refined TAIL h1, premium <= 10c, NO execution edge 5%, TAIL execution edge 5%. |
| tailh1_ref_p12_noe05_taile05 | $+354.93 | $+117.97 | $+53.74 | $+111.69 | $+71.52 | $+53.74 | 4/4 | 25.75% | 488 | Refined TAIL h1, premium <= 12c, NO execution edge 5%, TAIL execution edge 5%. |
| tailh1_ref_p15_noe05_taile05 | $+354.93 | $+117.97 | $+53.74 | $+111.69 | $+71.52 | $+53.74 | 4/4 | 25.75% | 488 | Refined TAIL h1, premium <= 15c, NO execution edge 5%, TAIL execution edge 5%. |
| tailh1_ref_p10_noe04_taile05 | $+353.25 | $+120.04 | $+53.74 | $+103.90 | $+75.56 | $+53.74 | 4/4 | 25.75% | 496 | Refined TAIL h1, premium <= 10c, NO execution edge 4%, TAIL execution edge 5%. |
| tailh1_ref_p12_noe04_taile05 | $+353.25 | $+120.04 | $+53.74 | $+103.90 | $+75.56 | $+53.74 | 4/4 | 25.75% | 496 | Refined TAIL h1, premium <= 12c, NO execution edge 4%, TAIL execution edge 5%. |
| tailh1_ref_p15_noe04_taile05 | $+353.25 | $+120.04 | $+53.74 | $+103.90 | $+75.56 | $+53.74 | 4/4 | 25.75% | 496 | Refined TAIL h1, premium <= 15c, NO execution edge 4%, TAIL execution edge 5%. |
| tailh1_ref_p08_noe05_taile05 | $+351.73 | $+117.97 | $+53.74 | $+111.69 | $+68.33 | $+53.74 | 4/4 | 25.75% | 485 | Refined TAIL h1, premium <= 8c, NO execution edge 5%, TAIL execution edge 5%. |
| tailh1_p07_noe05_taile05 | $+350.88 | $+117.97 | $+53.74 | $+110.84 | $+68.33 | $+53.74 | 4/4 | 25.75% | 484 | TAIL h1, premium <= 7c, NO execution edge 5%, TAIL execution edge 5%. |
| tailh1_ref_p07_noe05_taile05 | $+350.88 | $+117.97 | $+53.74 | $+110.84 | $+68.33 | $+53.74 | 4/4 | 25.75% | 484 | Refined TAIL h1, premium <= 7c, NO execution edge 5%, TAIL execution edge 5%. |
| sel_tail_mid | $+355.25 | $+130.08 | $+71.51 | $+100.36 | $+53.30 | $+53.30 | 4/4 | 21.00% | 467 | Selected L2 profile with interior TAIL only. |
| tailh1_p5_e7_tail_mid | $+326.02 | $+112.42 | $+59.59 | $+100.79 | $+53.22 | $+53.22 | 4/4 | 21.14% | 429 | TAIL h1 + 5c premium + 7pp edge + interior TAIL only. |
| sel_no_h0_4 | $+335.85 | $+114.22 | $+53.12 | $+90.24 | $+78.26 | $+53.12 | 4/4 | 22.62% | 413 | Selected L2 profile with NO local hours (0, 1, 2, 3, 4). |
| sel_taila40_fp03_cs40 | $+464.46 | $+116.26 | $+167.46 | $+128.19 | $+52.55 | $+52.55 | 4/4 | 19.49% | 440 | Selected L2 profile with TAIL alpha 4.0, fp_max 3%, consensus skip 40%. |
| sel_no_h0_3 | $+283.06 | $+102.99 | $+51.84 | $+57.59 | $+70.64 | $+51.84 | 4/4 | 19.50% | 376 | Selected L2 profile with NO local hours (0, 1, 2, 3). |
| tailh1_p06_noe05_taile05 | $+358.16 | $+116.50 | $+51.41 | $+121.92 | $+68.33 | $+51.41 | 4/4 | 26.86% | 480 | TAIL h1, premium <= 6c, NO execution edge 5%, TAIL execution edge 5%. |
| tailh1_ref_p09_noe06_taile05 | $+363.12 | $+111.95 | $+50.94 | $+120.93 | $+79.30 | $+50.94 | 4/4 | 27.08% | 476 | Refined TAIL h1, premium <= 9c, NO execution edge 6%, TAIL execution edge 5%. |
| tailh1_ref_p10_noe06_taile05 | $+363.12 | $+111.95 | $+50.94 | $+120.93 | $+79.30 | $+50.94 | 4/4 | 27.08% | 476 | Refined TAIL h1, premium <= 10c, NO execution edge 6%, TAIL execution edge 5%. |
| tailh1_ref_p12_noe06_taile05 | $+363.12 | $+111.95 | $+50.94 | $+120.93 | $+79.30 | $+50.94 | 4/4 | 27.08% | 476 | Refined TAIL h1, premium <= 12c, NO execution edge 6%, TAIL execution edge 5%. |
| tailh1_ref_p15_noe06_taile05 | $+363.12 | $+111.95 | $+50.94 | $+120.93 | $+79.30 | $+50.94 | 4/4 | 27.08% | 476 | Refined TAIL h1, premium <= 15c, NO execution edge 6%, TAIL execution edge 5%. |
| tailh1_ref_p08_noe06_taile05 | $+361.57 | $+111.95 | $+50.94 | $+120.93 | $+77.75 | $+50.94 | 4/4 | 27.08% | 475 | Refined TAIL h1, premium <= 8c, NO execution edge 6%, TAIL execution edge 5%. |
| tailh1_ref_p07_noe06_taile05 | $+360.68 | $+111.95 | $+50.94 | $+120.04 | $+77.75 | $+50.94 | 4/4 | 27.08% | 474 | Refined TAIL h1, premium <= 7c, NO execution edge 6%, TAIL execution edge 5%. |
| sel_taila45_fp05_cs60 | $+308.07 | $+110.07 | $+50.44 | $+76.34 | $+71.23 | $+50.44 | 4/4 | 27.31% | 494 | Selected L2 profile with TAIL alpha 4.5, fp_max 5%, consensus skip 60%. |
| sel_taila50_fp05_cs40 | $+368.63 | $+118.24 | $+94.65 | $+106.37 | $+49.37 | $+49.37 | 4/4 | 22.72% | 443 | Selected L2 profile with TAIL alpha 5.0, fp_max 5%, consensus skip 40%. |
| sel_taila50_fp07_cs40 | $+365.03 | $+118.24 | $+94.65 | $+102.77 | $+49.37 | $+49.37 | 4/4 | 22.72% | 444 | Selected L2 profile with TAIL alpha 5.0, fp_max 7%, consensus skip 40%. |
| sel_taila45_fp05_cs40 | $+353.96 | $+107.23 | $+94.65 | $+102.71 | $+49.37 | $+49.37 | 4/4 | 22.80% | 447 | Selected L2 profile with TAIL alpha 4.5, fp_max 5%, consensus skip 40%. |
| sel_taila45_fp07_cs40 | $+339.24 | $+100.26 | $+90.51 | $+99.11 | $+49.37 | $+49.37 | 4/4 | 22.80% | 451 | Selected L2 profile with TAIL alpha 4.5, fp_max 7%, consensus skip 40%. |
| sel_tail_h0_1 | $+261.98 | $+76.04 | $+49.03 | $+78.64 | $+58.27 | $+49.03 | 4/4 | 22.82% | 504 | Selected L2 profile with TAIL local hours (0, 1). |
| tailh1_p05_noe05_taile05 | $+345.51 | $+112.11 | $+48.58 | $+119.34 | $+65.47 | $+48.58 | 4/4 | 27.33% | 473 | TAIL h1, premium <= 5c, NO execution edge 5%, TAIL execution edge 5%. |
| sel_nofp80_nog09_20 | $+326.90 | $+104.38 | $+47.88 | $+91.39 | $+83.25 | $+47.88 | 4/4 | 21.25% | 386 | Selected L2 profile with NO fill >= 80%, NO edge [9%, 20%]. |
| sel_taila45_fp03_cs60 | $+297.47 | $+77.92 | $+47.78 | $+90.49 | $+81.28 | $+47.78 | 4/4 | 28.57% | 475 | Selected L2 profile with TAIL alpha 4.5, fp_max 3%, consensus skip 60%. |
| tailh1_p03_noe05_taile05 | $+336.02 | $+100.82 | $+47.53 | $+105.74 | $+81.93 | $+47.53 | 4/4 | 29.05% | 426 | TAIL h1, premium <= 3c, NO execution edge 5%, TAIL execution edge 5%. |
| tailh1_p03_noe07_taile05 | $+319.89 | $+91.11 | $+47.53 | $+101.92 | $+79.33 | $+47.53 | 4/4 | 29.05% | 415 | TAIL h1, premium <= 3c, NO execution edge 7%, TAIL execution edge 5%. |
| sel_taila45_fp07_cs60 | $+271.57 | $+83.71 | $+47.24 | $+69.40 | $+71.23 | $+47.24 | 4/4 | 28.37% | 504 | Selected L2 profile with TAIL alpha 4.5, fp_max 7%, consensus skip 60%. |
| sel_nofp80_nog09_15 | $+308.23 | $+94.86 | $+46.55 | $+95.30 | $+71.52 | $+46.55 | 4/4 | 21.95% | 372 | Selected L2 profile with NO fill >= 80%, NO edge [9%, 15%]. |
| sel_taila50_fp03_cs50 | $+297.93 | $+89.18 | $+64.80 | $+97.53 | $+46.43 | $+46.43 | 4/4 | 20.60% | 458 | Selected L2 profile with TAIL alpha 5.0, fp_max 3%, consensus skip 50%. |
| sel_taila40_fp05_cs40 | $+455.67 | $+126.28 | $+170.47 | $+112.67 | $+46.25 | $+46.25 | 4/4 | 22.49% | 457 | Selected L2 profile with TAIL alpha 4.0, fp_max 5%, consensus skip 40%. |
| sel_taila40_fp07_cs40 | $+437.74 | $+119.31 | $+163.12 | $+109.07 | $+46.25 | $+46.25 | 4/4 | 22.49% | 462 | Selected L2 profile with TAIL alpha 4.0, fp_max 7%, consensus skip 40%. |
| tailh1_p03_noe05_taile09 | $+298.39 | $+104.20 | $+68.22 | $+80.89 | $+45.08 | $+45.08 | 4/4 | 22.82% | 412 | TAIL h1, premium <= 3c, NO execution edge 5%, TAIL execution edge 9%. |
| tailh1_p03_noe05_taile11 | $+270.57 | $+73.66 | $+75.77 | $+76.06 | $+45.08 | $+45.08 | 4/4 | 24.51% | 399 | TAIL h1, premium <= 3c, NO execution edge 5%, TAIL execution edge 11%. |
| tailh1_p06_noe07_taile05 | $+339.47 | $+103.52 | $+45.04 | $+115.19 | $+75.71 | $+45.04 | 4/4 | 29.05% | 455 | TAIL h1, premium <= 6c, NO execution edge 7%, TAIL execution edge 5%. |
| tailh1_p07_noe07_taile05 | $+339.47 | $+103.52 | $+45.04 | $+115.19 | $+75.71 | $+45.04 | 4/4 | 29.05% | 455 | TAIL h1, premium <= 7c, NO execution edge 7%, TAIL execution edge 5%. |
| tailh1_p10_noe07_taile05 | $+339.47 | $+103.52 | $+45.04 | $+115.19 | $+75.71 | $+45.04 | 4/4 | 29.05% | 455 | TAIL h1, premium <= 10c, NO execution edge 7%, TAIL execution edge 5%. |
| premium5_exec7_tail_h2 | $+259.51 | $+69.63 | $+95.95 | $+44.31 | $+49.62 | $+44.31 | 4/4 | 22.82% | 459 | 5c premium + 7pp execution edge, TAIL local hour 2. |
| tailh1_p04_noe05_taile05 | $+344.52 | $+109.22 | $+43.75 | $+112.91 | $+78.64 | $+43.75 | 4/4 | 29.05% | 457 | TAIL h1, premium <= 4c, NO execution edge 5%, TAIL execution edge 5%. |
| sel_tail_h2 | $+287.17 | $+84.20 | $+109.98 | $+43.34 | $+49.65 | $+43.34 | 4/4 | 22.82% | 497 | Selected L2 profile with TAIL local hours (2,). |
| tailh1_p05_noe07_taile05 | $+329.94 | $+101.22 | $+43.32 | $+112.70 | $+72.70 | $+43.32 | 4/4 | 29.05% | 450 | TAIL h1, premium <= 5c, NO execution edge 7%, TAIL execution edge 5%. |
| sel_taila50_fp03_cs60 | $+266.70 | $+81.30 | $+47.78 | $+94.38 | $+43.24 | $+43.24 | 4/4 | 28.57% | 472 | Selected L2 profile with TAIL alpha 5.0, fp_max 3%, consensus skip 60%. |
| tailh1_ref_p09_noe06_taile09 | $+325.52 | $+115.33 | $+72.10 | $+94.87 | $+43.23 | $+43.23 | 4/4 | 22.76% | 462 | Refined TAIL h1, premium <= 9c, NO execution edge 6%, TAIL execution edge 9%. |
| tailh1_ref_p10_noe06_taile09 | $+325.52 | $+115.33 | $+72.10 | $+94.87 | $+43.23 | $+43.23 | 4/4 | 22.76% | 462 | Refined TAIL h1, premium <= 10c, NO execution edge 6%, TAIL execution edge 9%. |
| tailh1_ref_p12_noe06_taile09 | $+325.52 | $+115.33 | $+72.10 | $+94.87 | $+43.23 | $+43.23 | 4/4 | 22.76% | 462 | Refined TAIL h1, premium <= 12c, NO execution edge 6%, TAIL execution edge 9%. |
| tailh1_ref_p15_noe06_taile09 | $+325.52 | $+115.33 | $+72.10 | $+94.87 | $+43.23 | $+43.23 | 4/4 | 22.76% | 462 | Refined TAIL h1, premium <= 15c, NO execution edge 6%, TAIL execution edge 9%. |
| tailh1_p03_noe07_taile09 | $+283.55 | $+94.49 | $+68.22 | $+77.90 | $+42.94 | $+42.94 | 4/4 | 22.82% | 401 | TAIL h1, premium <= 3c, NO execution edge 7%, TAIL execution edge 9%. |
| tailh1_p03_noe07_taile11 | $+255.45 | $+64.45 | $+75.77 | $+72.30 | $+42.94 | $+42.94 | 4/4 | 24.50% | 388 | TAIL h1, premium <= 3c, NO execution edge 7%, TAIL execution edge 11%. |
| tailh1_p06_noe09_taile09 | $+245.67 | $+73.91 | $+48.23 | $+80.62 | $+42.91 | $+42.91 | 4/4 | 24.76% | 328 | TAIL h1, premium <= 6c, NO execution edge 9%, TAIL execution edge 9%. |
| tailh1_p07_noe09_taile09 | $+245.67 | $+73.91 | $+48.23 | $+80.62 | $+42.91 | $+42.91 | 4/4 | 24.76% | 328 | TAIL h1, premium <= 7c, NO execution edge 9%, TAIL execution edge 9%. |
| tailh1_p10_noe09_taile09 | $+245.67 | $+73.91 | $+48.23 | $+80.62 | $+42.91 | $+42.91 | 4/4 | 24.76% | 328 | TAIL h1, premium <= 10c, NO execution edge 9%, TAIL execution edge 9%. |
| tailh1_p06_noe09_taile11 | $+220.49 | $+46.80 | $+54.87 | $+75.91 | $+42.91 | $+42.91 | 4/4 | 22.93% | 315 | TAIL h1, premium <= 6c, NO execution edge 9%, TAIL execution edge 11%. |
| tailh1_p07_noe09_taile11 | $+220.49 | $+46.80 | $+54.87 | $+75.91 | $+42.91 | $+42.91 | 4/4 | 22.93% | 315 | TAIL h1, premium <= 7c, NO execution edge 9%, TAIL execution edge 11%. |
| tailh1_p10_noe09_taile11 | $+220.49 | $+46.80 | $+54.87 | $+75.91 | $+42.91 | $+42.91 | 4/4 | 22.93% | 315 | TAIL h1, premium <= 10c, NO execution edge 9%, TAIL execution edge 11%. |
| sel_nofp75_nog11_12 | $+237.44 | $+93.32 | $+42.79 | $+47.88 | $+53.44 | $+42.79 | 4/4 | 23.55% | 223 | Selected L2 profile with NO fill >= 75%, NO edge [11%, 12%]. |
| tailh1_p04_noe05_taile09 | $+305.57 | $+112.60 | $+63.69 | $+86.84 | $+42.44 | $+42.44 | 4/4 | 22.76% | 443 | TAIL h1, premium <= 4c, NO execution edge 5%, TAIL execution edge 9%. |
| tailh1_p04_noe05_taile11 | $+278.11 | $+81.80 | $+71.04 | $+82.83 | $+42.44 | $+42.44 | 4/4 | 22.82% | 430 | TAIL h1, premium <= 4c, NO execution edge 5%, TAIL execution edge 11%. |
| premium_le_2c | $+211.64 | $+45.88 | $+63.32 | $+60.13 | $+42.31 | $+42.31 | 4/4 | 24.59% | 387 | When L2 is present, require best ask <= displayed price + 2c. |
| premium2_exec5 | $+201.45 | $+47.25 | $+66.48 | $+45.40 | $+42.31 | $+42.31 | 4/4 | 24.63% | 381 | 2c L2 premium guard plus 5pp execution edge. |
| premium03_exec05 | $+240.71 | $+51.91 | $+76.88 | $+69.64 | $+42.28 | $+42.28 | 4/4 | 21.94% | 420 | 3c L2 premium guard plus 5% execution edge. |
| tailh1_p06_noe09_taile07 | $+272.02 | $+70.53 | $+42.09 | $+76.93 | $+82.47 | $+42.09 | 4/4 | 27.88% | 333 | TAIL h1, premium <= 6c, NO execution edge 9%, TAIL execution edge 7%. |
| tailh1_p07_noe09_taile07 | $+272.02 | $+70.53 | $+42.09 | $+76.93 | $+82.47 | $+42.09 | 4/4 | 27.88% | 333 | TAIL h1, premium <= 7c, NO execution edge 9%, TAIL execution edge 7%. |
| tailh1_p10_noe09_taile07 | $+272.02 | $+70.53 | $+42.09 | $+76.93 | $+82.47 | $+42.09 | 4/4 | 27.88% | 333 | TAIL h1, premium <= 10c, NO execution edge 9%, TAIL execution edge 7%. |
| tailh1_p04_noe07_taile05 | $+322.87 | $+98.55 | $+42.03 | $+108.82 | $+73.47 | $+42.03 | 4/4 | 29.05% | 441 | TAIL h1, premium <= 4c, NO execution edge 7%, TAIL execution edge 5%. |
| tailh1_ref_p08_noe06_taile09 | $+324.16 | $+115.33 | $+72.10 | $+94.87 | $+41.88 | $+41.88 | 4/4 | 22.76% | 461 | Refined TAIL h1, premium <= 8c, NO execution edge 6%, TAIL execution edge 9%. |
| tailh1_ref_p07_noe06_taile09 | $+323.30 | $+115.33 | $+72.10 | $+94.01 | $+41.88 | $+41.88 | 4/4 | 22.76% | 460 | Refined TAIL h1, premium <= 7c, NO execution edge 6%, TAIL execution edge 9%. |
| sel_nofp80_nog11_20 | $+249.05 | $+94.16 | $+41.80 | $+47.69 | $+65.40 | $+41.80 | 4/4 | 20.25% | 271 | Selected L2 profile with NO fill >= 80%, NO edge [11%, 20%]. |
| tailh1_p05_noe09_taile09 | $+239.68 | $+71.97 | $+46.44 | $+79.48 | $+41.78 | $+41.78 | 4/4 | 24.76% | 324 | TAIL h1, premium <= 5c, NO execution edge 9%, TAIL execution edge 9%. |
| tailh1_p05_noe09_taile11 | $+214.66 | $+45.17 | $+52.99 | $+74.71 | $+41.78 | $+41.78 | 4/4 | 22.93% | 311 | TAIL h1, premium <= 5c, NO execution edge 9%, TAIL execution edge 11%. |
| premium5_exec7_depth5c_ge5 | $+230.35 | $+57.01 | $+75.33 | $+56.79 | $+41.23 | $+41.23 | 4/4 | 21.32% | 426 | 5c premium + 7pp execution edge + $5 depth within 5c. |
| sel_no_h0_2 | $+227.09 | $+76.24 | $+41.18 | $+48.33 | $+61.35 | $+41.18 | 4/4 | 23.95% | 323 | Selected L2 profile with NO local hours (0, 1, 2). |
| sel_nofp75_nog11_20 | $+287.64 | $+105.83 | $+41.03 | $+69.48 | $+71.30 | $+41.03 | 4/4 | 24.27% | 407 | Selected L2 profile with NO fill >= 75%, NO edge [11%, 20%]. |
| depth5c_all_ge5 | $+246.18 | $+62.39 | $+77.18 | $+65.89 | $+40.73 | $+40.73 | 4/4 | 23.27% | 459 | Require at least $5 executable L2 ask depth within displayed+5c. |
| tailh1_ref_p09_noe04_taile09 | $+328.97 | $+123.42 | $+75.34 | $+89.49 | $+40.72 | $+40.72 | 4/4 | 22.64% | 481 | Refined TAIL h1, premium <= 9c, NO execution edge 4%, TAIL execution edge 9%. |
| tailh1_ref_p10_noe04_taile09 | $+319.77 | $+123.42 | $+75.34 | $+80.29 | $+40.72 | $+40.72 | 4/4 | 22.64% | 482 | Refined TAIL h1, premium <= 10c, NO execution edge 4%, TAIL execution edge 9%. |
| tailh1_ref_p12_noe04_taile09 | $+319.77 | $+123.42 | $+75.34 | $+80.29 | $+40.72 | $+40.72 | 4/4 | 22.64% | 482 | Refined TAIL h1, premium <= 12c, NO execution edge 4%, TAIL execution edge 9%. |
| tailh1_ref_p15_noe04_taile09 | $+319.77 | $+123.42 | $+75.34 | $+80.29 | $+40.72 | $+40.72 | 4/4 | 22.64% | 482 | Refined TAIL h1, premium <= 15c, NO execution edge 4%, TAIL execution edge 9%. |
| sel_nofp80_nog11_15 | $+229.74 | $+85.02 | $+40.55 | $+49.80 | $+54.37 | $+40.55 | 4/4 | 20.96% | 257 | Selected L2 profile with NO fill >= 80%, NO edge [11%, 15%]. |
| sel_spread_le_03c | $+290.55 | $+97.57 | $+66.49 | $+40.44 | $+86.04 | $+40.44 | 4/4 | 18.66% | 322 | Selected L2 profile requiring L2 spread <= 3c when bid+ask exist. |
| tailh1_p05_noe09_taile07 | $+265.89 | $+68.60 | $+40.41 | $+75.75 | $+81.13 | $+40.41 | 4/4 | 27.88% | 329 | TAIL h1, premium <= 5c, NO execution edge 9%, TAIL execution edge 7%. |
| premium03_exec07 | $+242.23 | $+47.13 | $+76.88 | $+78.05 | $+40.18 | $+40.18 | 4/4 | 21.17% | 405 | 3c L2 premium guard plus 7% execution edge. |
| premium5_noe03_taile11 | $+259.48 | $+46.10 | $+85.59 | $+88.36 | $+39.43 | $+39.43 | 4/4 | 22.04% | 450 | 5c premium guard with NO execution edge 3%, TAIL execution edge 11%. |
| tailh1_p04_noe09_taile09 | $+235.97 | $+71.97 | $+45.09 | $+79.48 | $+39.43 | $+39.43 | 4/4 | 24.76% | 322 | TAIL h1, premium <= 4c, NO execution edge 9%, TAIL execution edge 9%. |
| tailh1_p04_noe09_taile11 | $+210.83 | $+45.17 | $+51.53 | $+74.71 | $+39.43 | $+39.43 | 4/4 | 22.93% | 309 | TAIL h1, premium <= 4c, NO execution edge 9%, TAIL execution edge 11%. |
| premium04_exec05 | $+262.12 | $+58.86 | $+71.76 | $+92.13 | $+39.37 | $+39.37 | 4/4 | 21.17% | 452 | 4c L2 premium guard plus 5% execution edge. |
| no_premium2_tail_premium5_exec7 | $+215.96 | $+42.62 | $+66.48 | $+67.51 | $+39.35 | $+39.35 | 4/4 | 21.17% | 372 | NO premium <=2c, TAIL premium <=5c, with 7pp execution edge. |
| tailh1_p06_noe07_taile09 | $+300.09 | $+106.90 | $+65.16 | $+88.88 | $+39.15 | $+39.15 | 4/4 | 22.82% | 441 | TAIL h1, premium <= 6c, NO execution edge 7%, TAIL execution edge 9%. |
| tailh1_p07_noe07_taile09 | $+300.09 | $+106.90 | $+65.16 | $+88.88 | $+39.15 | $+39.15 | 4/4 | 22.82% | 441 | TAIL h1, premium <= 7c, NO execution edge 7%, TAIL execution edge 9%. |
| tailh1_p10_noe07_taile09 | $+300.09 | $+106.90 | $+65.16 | $+88.88 | $+39.15 | $+39.15 | 4/4 | 22.82% | 441 | TAIL h1, premium <= 10c, NO execution edge 7%, TAIL execution edge 9%. |
| tailh1_p06_noe07_taile11 | $+272.27 | $+75.63 | $+72.58 | $+84.91 | $+39.15 | $+39.15 | 4/4 | 22.82% | 428 | TAIL h1, premium <= 6c, NO execution edge 7%, TAIL execution edge 11%. |
| tailh1_p07_noe07_taile11 | $+272.27 | $+75.63 | $+72.58 | $+84.91 | $+39.15 | $+39.15 | 4/4 | 22.82% | 428 | TAIL h1, premium <= 7c, NO execution edge 7%, TAIL execution edge 11%. |
| tailh1_p10_noe07_taile11 | $+272.27 | $+75.63 | $+72.58 | $+84.91 | $+39.15 | $+39.15 | 4/4 | 22.82% | 428 | TAIL h1, premium <= 10c, NO execution edge 7%, TAIL execution edge 11%. |
| tailh1_p04_noe09_taile07 | $+261.61 | $+68.60 | $+39.14 | $+75.75 | $+78.12 | $+39.14 | 4/4 | 27.88% | 327 | TAIL h1, premium <= 4c, NO execution edge 9%, TAIL execution edge 7%. |
| sel_nofp70_nog07_12 | $+290.63 | $+92.32 | $+38.84 | $+82.76 | $+76.71 | $+38.84 | 4/4 | 26.60% | 636 | Selected L2 profile with NO fill >= 70%, NO edge [7%, 12%]. |
| premium5_all_fill75_exec7 | $+265.24 | $+55.12 | $+71.18 | $+100.64 | $+38.29 | $+38.29 | 4/4 | 21.17% | 438 | 5c premium + 7pp execution edge + require 75% target fill. |
| premium5_all_fill100_exec7 | $+265.24 | $+55.12 | $+71.18 | $+100.64 | $+38.29 | $+38.29 | 4/4 | 21.17% | 438 | 5c premium + 7pp execution edge + require full target fill. |
| exec_edge_9pp | $+223.31 | $+38.28 | $+58.10 | $+85.25 | $+41.68 | $+38.28 | 4/4 | 21.45% | 327 | NO and TAIL execution_min_edge = 9pp. |
| premium06_exec09 | $+223.31 | $+38.28 | $+58.10 | $+85.25 | $+41.68 | $+38.28 | 4/4 | 21.45% | 327 | 6c L2 premium guard plus 9% execution edge. |
| premium07_exec09 | $+223.31 | $+38.28 | $+58.10 | $+85.25 | $+41.68 | $+38.28 | 4/4 | 21.45% | 327 | 7c L2 premium guard plus 9% execution edge. |
| premium10_exec09 | $+223.31 | $+38.28 | $+58.10 | $+85.25 | $+41.68 | $+38.28 | 4/4 | 21.45% | 327 | 10c L2 premium guard plus 9% execution edge. |
| tailh1_p04_noe07_taile09 | $+285.40 | $+101.93 | $+61.62 | $+83.60 | $+38.26 | $+38.26 | 4/4 | 22.82% | 427 | TAIL h1, premium <= 4c, NO execution edge 7%, TAIL execution edge 9%. |
| tailh1_p04_noe07_taile11 | $+256.99 | $+70.95 | $+68.88 | $+78.90 | $+38.26 | $+38.26 | 4/4 | 22.82% | 414 | TAIL h1, premium <= 4c, NO execution edge 7%, TAIL execution edge 11%. |
| tailh1_ref_p08_noe04_taile09 | $+326.24 | $+123.42 | $+75.34 | $+89.49 | $+37.99 | $+37.99 | 4/4 | 22.64% | 478 | Refined TAIL h1, premium <= 8c, NO execution edge 4%, TAIL execution edge 9%. |
| tailh1_ref_p07_noe04_taile09 | $+325.40 | $+123.42 | $+75.34 | $+88.65 | $+37.99 | $+37.99 | 4/4 | 22.64% | 477 | Refined TAIL h1, premium <= 7c, NO execution edge 4%, TAIL execution edge 9%. |
| premium5_noe05_taile11 | $+254.03 | $+43.97 | $+85.59 | $+86.49 | $+37.98 | $+37.98 | 4/4 | 22.04% | 445 | 5c premium guard with NO execution edge 5%, TAIL execution edge 11%. |
| tailh1_p03_noe09_taile09 | $+223.21 | $+69.50 | $+43.66 | $+72.09 | $+37.96 | $+37.96 | 4/4 | 24.76% | 309 | TAIL h1, premium <= 3c, NO execution edge 9%, TAIL execution edge 9%. |
| tailh1_p03_noe09_taile11 | $+198.10 | $+43.08 | $+49.97 | $+67.10 | $+37.96 | $+37.96 | 4/4 | 23.95% | 296 | TAIL h1, premium <= 3c, NO execution edge 9%, TAIL execution edge 11%. |
| tailh1_p03_noe09_taile07 | $+248.78 | $+66.12 | $+37.79 | $+68.20 | $+76.66 | $+37.79 | 4/4 | 27.88% | 314 | TAIL h1, premium <= 3c, NO execution edge 9%, TAIL execution edge 7%. |
| sel_nofp80_nog11_12 | $+226.26 | $+96.24 | $+42.84 | $+37.63 | $+49.54 | $+37.63 | 4/4 | 22.09% | 186 | Selected L2 profile with NO fill >= 80%, NO edge [11%, 12%]. |
| sel_tail_h1_2 | $+297.26 | $+91.26 | $+91.76 | $+37.49 | $+76.75 | $+37.49 | 4/4 | 24.44% | 516 | Selected L2 profile with TAIL local hours (1, 2). |
| sel_taila50_fp05_cs50 | $+325.23 | $+133.76 | $+67.79 | $+86.29 | $+37.40 | $+37.40 | 4/4 | 22.48% | 473 | Selected L2 profile with TAIL alpha 5.0, fp_max 5%, consensus skip 50%. |
| tailh1_p10_noe05_taile09 | $+320.37 | $+121.35 | $+75.34 | $+86.29 | $+37.40 | $+37.40 | 4/4 | 22.67% | 474 | TAIL h1, premium <= 10c, NO execution edge 5%, TAIL execution edge 9%. |
| tailh1_ref_p09_noe05_taile09 | $+320.37 | $+121.35 | $+75.34 | $+86.29 | $+37.40 | $+37.40 | 4/4 | 22.67% | 474 | Refined TAIL h1, premium <= 9c, NO execution edge 5%, TAIL execution edge 9%. |
| tailh1_ref_p10_noe05_taile09 | $+320.37 | $+121.35 | $+75.34 | $+86.29 | $+37.40 | $+37.40 | 4/4 | 22.67% | 474 | Refined TAIL h1, premium <= 10c, NO execution edge 5%, TAIL execution edge 9%. |
| tailh1_ref_p12_noe05_taile09 | $+320.37 | $+121.35 | $+75.34 | $+86.29 | $+37.40 | $+37.40 | 4/4 | 22.67% | 474 | Refined TAIL h1, premium <= 12c, NO execution edge 5%, TAIL execution edge 9%. |
| tailh1_ref_p15_noe05_taile09 | $+320.37 | $+121.35 | $+75.34 | $+86.29 | $+37.40 | $+37.40 | 4/4 | 22.67% | 474 | Refined TAIL h1, premium <= 15c, NO execution edge 5%, TAIL execution edge 9%. |
| sel_taila50_fp07_cs50 | $+309.33 | $+121.37 | $+67.79 | $+82.79 | $+37.40 | $+37.40 | 4/4 | 22.67% | 477 | Selected L2 profile with TAIL alpha 5.0, fp_max 7%, consensus skip 50%. |
| tailh1_p10_noe05_taile11 | $+293.17 | $+90.20 | $+83.18 | $+82.39 | $+37.40 | $+37.40 | 4/4 | 22.82% | 461 | TAIL h1, premium <= 10c, NO execution edge 5%, TAIL execution edge 11%. |
| premium5_exec9 | $+217.60 | $+36.74 | $+56.22 | $+84.05 | $+40.58 | $+36.74 | 4/4 | 21.45% | 323 | 5c L2 premium guard plus 9pp execution edge. |
| premium5_noe09_taile09 | $+217.60 | $+36.74 | $+56.22 | $+84.05 | $+40.58 | $+36.74 | 4/4 | 21.45% | 323 | 5c premium guard with NO execution edge 9%, TAIL execution edge 9%. |
| premium04_exec09 | $+213.65 | $+36.74 | $+54.62 | $+84.05 | $+38.24 | $+36.74 | 4/4 | 21.45% | 321 | 4c L2 premium guard plus 9% execution edge. |
| tailh1_p05_noe07_taile09 | $+291.19 | $+104.60 | $+63.17 | $+86.72 | $+36.71 | $+36.71 | 4/4 | 22.82% | 436 | TAIL h1, premium <= 5c, NO execution edge 7%, TAIL execution edge 9%. |
| tailh1_p05_noe07_taile11 | $+263.40 | $+73.67 | $+70.51 | $+82.51 | $+36.71 | $+36.71 | 4/4 | 22.82% | 423 | TAIL h1, premium <= 5c, NO execution edge 7%, TAIL execution edge 11%. |
| premium5_exec7_depth5c_ge1 | $+269.56 | $+58.44 | $+71.18 | $+103.32 | $+36.63 | $+36.63 | 4/4 | 21.17% | 439 | 5c premium + 7pp execution edge + $1 depth within 5c. |
| premium5_exec7_depth5c_ge3 | $+257.53 | $+58.44 | $+70.28 | $+92.18 | $+36.63 | $+36.63 | 4/4 | 21.17% | 436 | 5c premium + 7pp execution edge + $3 depth within 5c. |
| premium5_exec7_depth5c_ge2 | $+253.29 | $+58.44 | $+70.28 | $+87.95 | $+36.63 | $+36.63 | 4/4 | 21.17% | 437 | 5c premium + 7pp execution edge + $2 depth within 5c. |
| no_premium5_tail_premium2_exec7 | $+250.88 | $+55.12 | $+71.18 | $+87.95 | $+36.63 | $+36.63 | 4/4 | 21.17% | 439 | NO premium <=5c, TAIL premium <=2c, with 7pp execution edge. |
| exec_edge_7pp | $+271.95 | $+56.87 | $+73.23 | $+105.77 | $+36.08 | $+36.08 | 4/4 | 21.17% | 446 | NO and TAIL execution_min_edge = 7pp. |
| premium06_exec07 | $+271.95 | $+56.87 | $+73.23 | $+105.77 | $+36.08 | $+36.08 | 4/4 | 21.17% | 446 | 6c L2 premium guard plus 7% execution edge. |
| premium07_exec07 | $+271.95 | $+56.87 | $+73.23 | $+105.77 | $+36.08 | $+36.08 | 4/4 | 21.17% | 446 | 7c L2 premium guard plus 7% execution edge. |
| premium10_exec07 | $+271.95 | $+56.87 | $+73.23 | $+105.77 | $+36.08 | $+36.08 | 4/4 | 21.17% | 446 | 10c L2 premium guard plus 7% execution edge. |
| tail_premium5_exec7 | $+271.95 | $+56.87 | $+73.23 | $+105.77 | $+36.08 | $+36.08 | 4/4 | 21.17% | 446 | Apply 5c premium guard only to TAIL, with 7pp execution edge. |
| sel_nofp75_nog11_15 | $+241.39 | $+77.74 | $+36.02 | $+64.49 | $+63.15 | $+36.02 | 4/4 | 28.09% | 349 | Selected L2 profile with NO fill >= 75%, NO edge [11%, 15%]. |
| premium5_noe07_taile11 | $+236.08 | $+35.60 | $+78.44 | $+79.29 | $+42.75 | $+35.60 | 4/4 | 22.04% | 422 | 5c premium guard with NO execution edge 7%, TAIL execution edge 11%. |
| premium04_exec07 | $+257.41 | $+52.80 | $+69.44 | $+99.87 | $+35.31 | $+35.31 | 4/4 | 21.17% | 432 | 4c L2 premium guard plus 7% execution edge. |
| premium5_exec7_no_h0_3 | $+219.30 | $+49.32 | $+58.30 | $+76.71 | $+34.98 | $+34.98 | 4/4 | 18.00% | 350 | 5c premium + 7pp execution edge, NO local hours 0-3. |
| premium5_noe07_taile09 | $+263.97 | $+61.66 | $+75.26 | $+92.06 | $+34.98 | $+34.98 | 4/4 | 21.17% | 435 | 5c premium guard with NO execution edge 7%, TAIL execution edge 9%. |
| premium03_exec09 | $+186.04 | $+34.78 | $+52.91 | $+61.49 | $+36.86 | $+34.78 | 4/4 | 22.20% | 307 | 3c L2 premium guard plus 9% execution edge. |
| tailh1_p06_noe05_taile09 | $+322.26 | $+119.88 | $+72.64 | $+95.01 | $+34.74 | $+34.74 | 4/4 | 22.69% | 466 | TAIL h1, premium <= 6c, NO execution edge 5%, TAIL execution edge 9%. |
| tailh1_ref_p08_noe05_taile09 | $+317.72 | $+121.35 | $+75.34 | $+86.29 | $+34.74 | $+34.74 | 4/4 | 22.67% | 471 | Refined TAIL h1, premium <= 8c, NO execution edge 5%, TAIL execution edge 9%. |
| tailh1_p07_noe05_taile09 | $+316.92 | $+121.35 | $+75.34 | $+85.50 | $+34.74 | $+34.74 | 4/4 | 22.67% | 470 | TAIL h1, premium <= 7c, NO execution edge 5%, TAIL execution edge 9%. |
| tailh1_ref_p07_noe05_taile09 | $+316.92 | $+121.35 | $+75.34 | $+85.50 | $+34.74 | $+34.74 | 4/4 | 22.67% | 470 | Refined TAIL h1, premium <= 7c, NO execution edge 5%, TAIL execution edge 9%. |
| tailh1_p06_noe05_taile11 | $+295.79 | $+88.73 | $+80.36 | $+91.96 | $+34.74 | $+34.74 | 4/4 | 22.82% | 453 | TAIL h1, premium <= 6c, NO execution edge 5%, TAIL execution edge 11%. |
| tailh1_p07_noe05_taile11 | $+289.58 | $+90.20 | $+83.18 | $+81.46 | $+34.74 | $+34.74 | 4/4 | 22.82% | 457 | TAIL h1, premium <= 7c, NO execution edge 5%, TAIL execution edge 11%. |
| sel_taila50_fp05_cs60 | $+290.38 | $+125.83 | $+50.44 | $+79.70 | $+34.41 | $+34.41 | 4/4 | 27.31% | 488 | Selected L2 profile with TAIL alpha 5.0, fp_max 5%, consensus skip 60%. |
| sel_taila50_fp07_cs60 | $+266.97 | $+105.80 | $+50.44 | $+76.32 | $+34.41 | $+34.41 | 4/4 | 27.31% | 494 | Selected L2 profile with TAIL alpha 5.0, fp_max 7%, consensus skip 60%. |
| sel_tail_h0 | $+291.11 | $+69.56 | $+84.66 | $+102.55 | $+34.34 | $+34.34 | 4/4 | 21.17% | 479 | Selected L2 profile with TAIL local hours (0,). |
| exec_edge_5pp | $+276.53 | $+66.11 | $+84.66 | $+91.42 | $+34.34 | $+34.34 | 4/4 | 21.17% | 483 | NO and TAIL execution_min_edge = 5pp. |
| premium10_exec05 | $+276.53 | $+66.11 | $+84.66 | $+91.42 | $+34.34 | $+34.34 | 4/4 | 21.17% | 483 | 10c L2 premium guard plus 5% execution edge. |
| sel_no_h0 | $+194.48 | $+78.05 | $+36.10 | $+46.36 | $+33.97 | $+33.97 | 4/4 | 26.76% | 191 | Selected L2 profile with NO local hours (0,). |
| tailh1_ref_p09_noe03_taile09 | $+328.26 | $+124.32 | $+75.63 | $+94.40 | $+33.91 | $+33.91 | 4/4 | 22.63% | 489 | Refined TAIL h1, premium <= 9c, NO execution edge 3%, TAIL execution edge 9%. |
| tailh1_ref_p10_noe03_taile09 | $+318.53 | $+124.32 | $+75.63 | $+84.67 | $+33.91 | $+33.91 | 4/4 | 22.63% | 490 | Refined TAIL h1, premium <= 10c, NO execution edge 3%, TAIL execution edge 9%. |
| tailh1_ref_p12_noe03_taile09 | $+318.53 | $+124.32 | $+75.63 | $+84.67 | $+33.91 | $+33.91 | 4/4 | 22.63% | 490 | Refined TAIL h1, premium <= 12c, NO execution edge 3%, TAIL execution edge 9%. |
| tailh1_ref_p15_noe03_taile09 | $+318.53 | $+124.32 | $+75.63 | $+84.67 | $+33.91 | $+33.91 | 4/4 | 22.63% | 490 | Refined TAIL h1, premium <= 15c, NO execution edge 3%, TAIL execution edge 9%. |
| premium5_exec7_no_h1_6 | $+253.12 | $+54.45 | $+77.24 | $+87.54 | $+33.89 | $+33.89 | 4/4 | 17.05% | 422 | 5c premium + 7pp execution edge, NO local hours 1-6. |
| premium5_exec7_tail_h0_2 | $+197.14 | $+39.25 | $+63.27 | $+33.73 | $+60.89 | $+33.73 | 4/4 | 26.04% | 502 | 5c premium + 7pp execution edge, TAIL local hours 0-2. |
| premium5_exec7 | $+263.31 | $+55.12 | $+71.18 | $+103.32 | $+33.69 | $+33.69 | 4/4 | 21.17% | 441 | 5c L2 premium guard plus 7pp execution edge. |
| premium5_noe07_taile07 | $+263.31 | $+55.12 | $+71.18 | $+103.32 | $+33.69 | $+33.69 | 4/4 | 21.17% | 441 | 5c premium guard with NO execution edge 7%, TAIL execution edge 7%. |
| no_premium5_exec7 | $+263.31 | $+55.12 | $+71.18 | $+103.32 | $+33.69 | $+33.69 | 4/4 | 21.17% | 441 | Apply 5c premium guard only to NO, with 7pp execution edge. |
| premium5_noe07_taile03 | $+256.87 | $+48.83 | $+68.01 | $+106.34 | $+33.69 | $+33.69 | 4/4 | 21.17% | 448 | 5c premium guard with NO execution edge 7%, TAIL execution edge 3%. |
| premium5_noe07_taile05 | $+248.79 | $+52.02 | $+71.18 | $+91.90 | $+33.69 | $+33.69 | 4/4 | 21.17% | 445 | 5c premium guard with NO execution edge 7%, TAIL execution edge 5%. |
| depth5c_all_ge1 | $+290.09 | $+63.89 | $+77.10 | $+115.43 | $+33.66 | $+33.66 | 4/4 | 21.17% | 475 | Require at least $1 executable L2 ask depth within displayed+5c. |
| depth5c_all_ge3 | $+277.52 | $+63.89 | $+76.17 | $+103.80 | $+33.66 | $+33.66 | 4/4 | 21.17% | 472 | Require at least $3 executable L2 ask depth within displayed+5c. |
| depth5c_all_ge2 | $+273.10 | $+63.89 | $+76.17 | $+99.38 | $+33.66 | $+33.66 | 4/4 | 21.17% | 473 | Require at least $2 executable L2 ask depth within displayed+5c. |
| premium5_exec7_enable_yhigh | $+260.77 | $+54.44 | $+72.83 | $+100.27 | $+33.23 | $+33.23 | 4/4 | 20.66% | 458 | 5c premium + 7pp NO/TAIL edge, re-enable YHIGH under L2 execution. |
| sel_tail_h0_2 | $+221.72 | $+51.98 | $+76.39 | $+32.63 | $+60.73 | $+32.63 | 4/4 | 25.54% | 540 | Selected L2 profile with TAIL local hours (0, 1, 2). |
| tailh1_p05_noe05_taile09 | $+310.04 | $+115.49 | $+69.42 | $+92.74 | $+32.39 | $+32.39 | 4/4 | 22.72% | 459 | TAIL h1, premium <= 5c, NO execution edge 5%, TAIL execution edge 9%. |
| tailh1_p05_noe05_taile11 | $+283.57 | $+84.70 | $+77.01 | $+89.47 | $+32.39 | $+32.39 | 4/4 | 22.82% | 446 | TAIL h1, premium <= 5c, NO execution edge 5%, TAIL execution edge 11%. |
| premium5_noe03_taile09 | $+289.26 | $+74.00 | $+82.42 | $+100.71 | $+32.13 | $+32.13 | 4/4 | 21.17% | 463 | 5c premium guard with NO execution edge 3%, TAIL execution edge 9%. |
| sel_nofp70_nog09_12 | $+310.75 | $+104.29 | $+31.99 | $+83.48 | $+90.99 | $+31.99 | 4/4 | 26.63% | 472 | Selected L2 profile with NO fill >= 70%, NO edge [9%, 12%]. |
| premium06_exec05 | $+279.27 | $+64.86 | $+81.61 | $+100.99 | $+31.80 | $+31.80 | 4/4 | 21.17% | 475 | 6c L2 premium guard plus 5% execution edge. |
| premium07_exec05 | $+273.14 | $+66.11 | $+84.66 | $+90.57 | $+31.80 | $+31.80 | 4/4 | 21.17% | 479 | 7c L2 premium guard plus 5% execution edge. |
| tailh1_ref_p08_noe03_taile09 | $+325.66 | $+124.32 | $+75.63 | $+94.40 | $+31.31 | $+31.31 | 4/4 | 22.63% | 486 | Refined TAIL h1, premium <= 8c, NO execution edge 3%, TAIL execution edge 9%. |
| premium5_noe09_taile07 | $+217.68 | $+31.17 | $+52.54 | $+94.81 | $+39.17 | $+31.17 | 4/4 | 21.45% | 329 | 5c premium guard with NO execution edge 9%, TAIL execution edge 7%. |
| no_only_exec_5pp | $+157.32 | $+34.90 | $+31.08 | $+58.45 | $+32.89 | $+31.08 | 4/4 | 20.03% | 400 | NO only plus 5pp execution edge. |
| sel_tail_low | $+154.05 | $+34.90 | $+31.08 | $+55.19 | $+32.89 | $+31.08 | 4/4 | 20.03% | 401 | Selected L2 profile with low/open-cold TAIL only. |
| tailh1_ref_p07_noe03_taile09 | $+324.15 | $+124.32 | $+75.63 | $+93.15 | $+31.05 | $+31.05 | 4/4 | 22.63% | 483 | Refined TAIL h1, premium <= 7c, NO execution edge 3%, TAIL execution edge 9%. |
| baseline_l2 | $+281.66 | $+65.36 | $+81.77 | $+103.65 | $+30.88 | $+30.88 | 4/4 | 24.44% | 502 | Current champion with L2 ask-ladder entry execution. |
| no_fill_50pct | $+281.66 | $+65.36 | $+81.77 | $+103.65 | $+30.88 | $+30.88 | 4/4 | 24.44% | 502 | Require NO real fill >= 50% of target. |
| no_fill_75pct | $+281.66 | $+65.36 | $+81.77 | $+103.65 | $+30.88 | $+30.88 | 4/4 | 24.44% | 502 | Require NO real fill >= 75% of target. |
| tail_fill_50pct | $+281.66 | $+65.36 | $+81.77 | $+103.65 | $+30.88 | $+30.88 | 4/4 | 24.44% | 502 | Require TAIL real fill >= 50% of target. |
| all_fill_50pct | $+281.66 | $+65.36 | $+81.77 | $+103.65 | $+30.88 | $+30.88 | 4/4 | 24.44% | 502 | Require every real fill >= 50% of target. |
| require_l2 | $+276.24 | $+59.94 | $+81.77 | $+103.65 | $+30.88 | $+30.88 | 4/4 | 24.44% | 440 | Evaluate only bets with an L2 ladder in the parquet. |
| premium5_noe03_taile07 | $+287.84 | $+67.16 | $+78.19 | $+111.61 | $+30.88 | $+30.88 | 4/4 | 21.17% | 469 | 5c premium guard with NO execution edge 3%, TAIL execution edge 7%. |
| premium_le_5c | $+280.95 | $+60.52 | $+75.01 | $+114.53 | $+30.88 | $+30.88 | 4/4 | 21.17% | 476 | When L2 is present, require best ask <= displayed price + 5c. |
| premium5_noe03_taile05 | $+272.91 | $+63.75 | $+78.19 | $+100.09 | $+30.88 | $+30.88 | 4/4 | 21.17% | 473 | 5c premium guard with NO execution edge 3%, TAIL execution edge 5%. |
| tailh1_p06_noe09_taile05 | $+286.35 | $+70.53 | $+30.76 | $+106.05 | $+79.01 | $+30.76 | 4/4 | 33.72% | 342 | TAIL h1, premium <= 6c, NO execution edge 9%, TAIL execution edge 5%. |
| tailh1_p07_noe09_taile05 | $+286.35 | $+70.53 | $+30.76 | $+106.05 | $+79.01 | $+30.76 | 4/4 | 33.72% | 342 | TAIL h1, premium <= 7c, NO execution edge 9%, TAIL execution edge 5%. |
| tailh1_p10_noe09_taile05 | $+286.35 | $+70.53 | $+30.76 | $+106.05 | $+79.01 | $+30.76 | 4/4 | 33.72% | 342 | TAIL h1, premium <= 10c, NO execution edge 9%, TAIL execution edge 5%. |
| premium5_noe05_taile09 | $+283.54 | $+71.49 | $+82.42 | $+98.88 | $+30.76 | $+30.76 | 4/4 | 21.17% | 458 | 5c premium guard with NO execution edge 5%, TAIL execution edge 9%. |
| premium5_noe05_taile07 | $+282.38 | $+64.70 | $+78.19 | $+109.98 | $+29.52 | $+29.52 | 4/4 | 21.17% | 464 | 5c premium guard with NO execution edge 5%, TAIL execution edge 7%. |
| premium5_noe05_taile03 | $+275.56 | $+58.10 | $+75.01 | $+112.92 | $+29.52 | $+29.52 | 4/4 | 21.17% | 471 | 5c premium guard with NO execution edge 5%, TAIL execution edge 3%. |
| premium5_exec5 | $+267.52 | $+61.33 | $+78.19 | $+98.48 | $+29.52 | $+29.52 | 4/4 | 21.17% | 468 | 5c L2 premium guard plus 5pp execution edge. |
| premium5_noe05_taile05 | $+267.52 | $+61.33 | $+78.19 | $+98.48 | $+29.52 | $+29.52 | 4/4 | 21.17% | 468 | 5c premium guard with NO execution edge 5%, TAIL execution edge 5%. |
| no_only | $+155.08 | $+36.95 | $+31.28 | $+57.38 | $+29.47 | $+29.47 | 4/4 | 20.03% | 416 | Disable TAIL; NO sleeve only. |
| tail_low_only | $+151.84 | $+36.95 | $+31.28 | $+54.14 | $+29.47 | $+29.47 | 4/4 | 20.03% | 417 | Keep only low/open-cold TAIL bets. |
| tailh1_p05_noe09_taile05 | $+280.36 | $+68.60 | $+29.21 | $+104.85 | $+77.70 | $+29.21 | 4/4 | 33.72% | 338 | TAIL h1, premium <= 5c, NO execution edge 9%, TAIL execution edge 5%. |
| premium5_noe09_taile05 | $+203.72 | $+28.51 | $+52.54 | $+83.51 | $+39.17 | $+28.51 | 4/4 | 21.65% | 333 | 5c premium guard with NO execution edge 9%, TAIL execution edge 5%. |
| tail_far_abs_ge3 | $+226.22 | $+62.65 | $+28.46 | $+92.71 | $+42.40 | $+28.46 | 4/4 | 19.48% | 455 | Keep TAIL only at least three brackets away from the market-implied mode. |
| tailh1_p04_noe09_taile05 | $+276.24 | $+68.60 | $+28.04 | $+104.85 | $+74.74 | $+28.04 | 4/4 | 33.72% | 336 | TAIL h1, premium <= 4c, NO execution edge 9%, TAIL execution edge 5%. |
| premium5_tail_mid_exec9 | $+245.87 | $+47.50 | $+63.86 | $+106.47 | $+28.04 | $+28.04 | 4/4 | 17.50% | 308 | 5c L2 premium guard plus interior TAIL only plus 9pp execution edge. |
| sel_no_h0_1 | $+172.72 | $+57.79 | $+32.78 | $+54.42 | $+27.74 | $+27.74 | 4/4 | 25.37% | 247 | Selected L2 profile with NO local hours (0, 1). |
| sel_tail_near_mode_abs_le2 | $+193.34 | $+36.06 | $+77.31 | $+52.57 | $+27.40 | $+27.40 | 4/4 | 22.82% | 443 | Selected L2 profile with TAIL within two brackets of market-implied mode. |
| tailh1_p03_noe09_taile05 | $+263.32 | $+66.12 | $+26.80 | $+96.96 | $+73.43 | $+26.80 | 4/4 | 33.72% | 323 | TAIL h1, premium <= 3c, NO execution edge 9%, TAIL execution edge 5%. |
| premium5_no_only | $+154.77 | $+32.99 | $+26.79 | $+65.53 | $+29.46 | $+26.79 | 4/4 | 20.03% | 390 | 5c L2 premium guard with NO sleeve only. |
| premium5_no_only_exec5 | $+150.03 | $+31.00 | $+26.79 | $+64.12 | $+28.12 | $+26.79 | 4/4 | 20.03% | 385 | 5c L2 premium guard with NO only plus 5pp execution edge. |
| sel_tail_high | $+153.36 | $+26.64 | $+28.35 | $+45.68 | $+52.69 | $+26.64 | 4/4 | 21.71% | 411 | Selected L2 profile with high/open-hot TAIL only. |
| premium_le_1c | $+169.77 | $+45.13 | $+40.25 | $+57.96 | $+26.42 | $+26.42 | 4/4 | 18.55% | 277 | When L2 is present, require best ask <= displayed price + 1c. |
| sel_nofp70_nog11_12 | $+263.83 | $+100.60 | $+26.12 | $+53.76 | $+83.36 | $+26.12 | 4/4 | 22.26% | 255 | Selected L2 profile with NO fill >= 70%, NO edge [11%, 12%]. |
| premium5_noe09_taile03 | $+212.14 | $+25.81 | $+49.39 | $+97.78 | $+39.17 | $+25.81 | 4/4 | 21.65% | 336 | 5c premium guard with NO execution edge 9%, TAIL execution edge 3%. |
| right_center_exec_5pp | $+265.95 | $+66.11 | $+25.41 | $+131.32 | $+43.12 | $+25.41 | 4/4 | 21.17% | 456 | TAIL at/above mode plus 5pp execution edge. |
| sel_tail_far_abs_ge3 | $+310.85 | $+116.85 | $+23.79 | $+88.81 | $+81.40 | $+23.79 | 4/4 | 19.48% | 436 | Selected L2 profile with TAIL at least three brackets from market-implied mode. |
| tail_mid_exec7 | $+306.87 | $+73.91 | $+81.60 | $+128.05 | $+23.30 | $+23.30 | 4/4 | 19.48% | 430 | Interior TAIL only plus 7pp execution edge. |
| tail_right_center | $+255.69 | $+65.36 | $+22.97 | $+127.94 | $+39.43 | $+22.97 | 4/4 | 21.17% | 474 | Keep TAIL only at/above the market-implied mode. |
| tail_right_only | $+255.69 | $+65.36 | $+22.97 | $+127.94 | $+39.43 | $+22.97 | 4/4 | 21.17% | 474 | Keep TAIL only hotter than the market-implied mode. |
| tailh1_p5_e7_tail_low | $+133.64 | $+23.17 | $+22.35 | $+55.93 | $+32.19 | $+22.35 | 4/4 | 20.03% | 363 | TAIL h1 + 5c premium + 7pp edge + low/open-cold TAIL only. |
| tail_mid_exec5 | $+316.93 | $+84.16 | $+93.53 | $+117.11 | $+22.14 | $+22.14 | 4/4 | 19.48% | 466 | Interior TAIL only plus 5pp execution edge. |
| top_all_ge1 | $+137.11 | $+38.29 | $+38.12 | $+39.17 | $+21.52 | $+21.52 | 4/4 | 22.90% | 444 | Require best ask level notional >= $1. |
| premium5_tail_mid_exec7 | $+297.99 | $+71.94 | $+79.47 | $+125.47 | $+21.12 | $+21.12 | 4/4 | 19.48% | 425 | 5c L2 premium guard plus interior TAIL only plus 7pp execution edge. |
| tail_high_only | $+127.72 | $+20.67 | $+25.86 | $+38.79 | $+42.40 | $+20.67 | 4/4 | 21.71% | 433 | Keep only high/open-hot TAIL bets. |
| premium5_exec7_enable_ymid | $+197.99 | $+45.76 | $+61.60 | $+70.61 | $+20.02 | $+20.02 | 4/4 | 21.98% | 618 | 5c premium + 7pp NO/TAIL edge, re-enable YMID under L2 execution. |
| premium5_exec7_enable_ymid_yhigh | $+197.38 | $+45.12 | $+63.16 | $+69.48 | $+19.61 | $+19.61 | 4/4 | 21.48% | 634 | 5c premium + 7pp NO/TAIL edge, re-enable YMID and YHIGH under L2 execution. |
| tail_near_mode_abs_le2 | $+206.70 | $+38.18 | $+83.22 | $+66.31 | $+18.99 | $+18.99 | 4/4 | 21.17% | 463 | Keep TAIL only within two brackets of the market-implied mode. |
| tail_mid_only | $+325.36 | $+86.88 | $+90.66 | $+128.84 | $+18.99 | $+18.99 | 4/4 | 22.05% | 484 | Keep only interior TAIL bets. |
| premium5_tail_mid | $+325.24 | $+81.56 | $+83.60 | $+141.09 | $+18.99 | $+18.99 | 4/4 | 19.48% | 458 | 5c L2 premium guard plus interior TAIL only. |
| premium5_tail_mid_exec5 | $+308.35 | $+78.88 | $+86.77 | $+124.95 | $+17.75 | $+17.75 | 4/4 | 19.48% | 451 | 5c L2 premium guard plus interior TAIL only plus 5pp execution edge. |
| premium5_exec7_tail_h3 | $+222.86 | $+60.21 | $+64.16 | $+16.67 | $+81.82 | $+16.67 | 4/4 | 28.12% | 486 | 5c premium + 7pp execution edge, TAIL local hour 3. |
| tailh1_p5_e7_tail_high | $+134.33 | $+15.62 | $+19.80 | $+46.39 | $+52.52 | $+15.62 | 4/4 | 21.71% | 373 | TAIL h1 + 5c premium + 7pp edge + high/open-hot TAIL only. |
| premium5_noe09_taile11 | $+193.36 | $+14.54 | $+59.37 | $+70.96 | $+48.49 | $+14.54 | 4/4 | 27.22% | 310 | 5c premium guard with NO execution edge 9%, TAIL execution edge 11%. |
| sel_nofp70_nog11_20 | $+364.77 | $+147.79 | $+13.31 | $+125.94 | $+77.74 | $+13.31 | 4/4 | 29.46% | 516 | Selected L2 profile with NO fill >= 70%, NO edge [11%, 20%]. |
| sel_nofp70_nog07_20 | $+368.95 | $+134.83 | $+12.39 | $+150.96 | $+70.78 | $+12.39 | 4/4 | 34.93% | 800 | Selected L2 profile with NO fill >= 70%, NO edge [7%, 20%]. |
| top_all_ge3 | $+145.70 | $+10.94 | $+40.43 | $+70.22 | $+24.11 | $+10.94 | 4/4 | 20.03% | 427 | Require best ask level notional >= $3. |
| top_all_ge2 | $+142.20 | $+10.94 | $+40.43 | $+66.72 | $+24.11 | $+10.94 | 4/4 | 20.03% | 428 | Require best ask level notional >= $2. |
| sel_nofp70_nog07_15 | $+322.54 | $+107.70 | $+10.19 | $+130.73 | $+73.92 | $+10.19 | 4/4 | 37.50% | 727 | Selected L2 profile with NO fill >= 70%, NO edge [7%, 15%]. |
| sel_nofp70_nog11_15 | $+293.21 | $+113.79 | $+9.84 | $+90.43 | $+79.16 | $+9.84 | 4/4 | 32.07% | 427 | Selected L2 profile with NO fill >= 70%, NO edge [11%, 15%]. |
| premium5_exec7_tail_h5 | $+144.51 | $+70.31 | $+9.53 | $+19.20 | $+45.47 | $+9.53 | 4/4 | 30.50% | 480 | 5c premium + 7pp execution edge, TAIL local hour 5. |
| premium5_exec7_tail_h6 | $+136.83 | $+9.41 | $+19.89 | $+21.09 | $+86.44 | $+9.41 | 4/4 | 42.69% | 494 | 5c premium + 7pp execution edge, TAIL local hour 6. |
| top_all_ge5 | $+116.16 | $+9.04 | $+24.42 | $+60.00 | $+22.70 | $+9.04 | 4/4 | 20.56% | 400 | Require best ask level notional >= $5. |
| sel_nofp70_nog09_20 | $+381.86 | $+157.65 | $+2.61 | $+135.30 | $+86.30 | $+2.61 | 4/4 | 36.62% | 655 | Selected L2 profile with NO fill >= 70%, NO edge [9%, 20%]. |
| premium5_exec7_no_h0 | $+138.01 | $+34.46 | $+43.52 | $+58.41 | $+1.62 | $+1.62 | 4/4 | 20.48% | 182 | 5c premium + 7pp execution edge, NO only at local hour 0. |
| sel_nofp70_nog09_15 | $+338.29 | $+132.44 | $+0.59 | $+115.02 | $+90.24 | $+0.59 | 4/4 | 39.13% | 578 | Selected L2 profile with NO fill >= 70%, NO edge [9%, 15%]. |
| premium5_exec7_tail_h4 | $+126.15 | $+57.38 | $-3.70 | $+40.28 | $+32.19 | $-3.70 | 3/4 | 29.62% | 490 | 5c premium + 7pp execution edge, TAIL local hour 4. |
| premium_le_0 | $+25.49 | $-0.86 | $+5.25 | $+26.22 | $-5.12 | $-5.12 | 2/4 | 16.73% | 132 | When L2 is present, require best ask <= displayed price. |
