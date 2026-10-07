# TAIL Delayed Entry WFO Sweep

Protocol: normal TAIL condition must be satisfied first; then TAIL waits for YES price history to hit the threshold before entry. NO is unchanged. Delayed entries must still be at least 4h before bracket close. B-D is one continuous OOS bankroll path.

## Chunks

- A: 2026-02-18 to 2026-03-12
- B: 2026-03-13 to 2026-04-04
- C: 2026-04-05 to 2026-04-27
- D: 2026-04-28 to 2026-05-21

## Baseline

- Immediate TAIL: PnL $+1319.71; TAIL $+469.72 on 31 bets; max DD 26.22%.

## Variants

| rank | variant | PnL | maxDD | worst chunk | n | NO PnL | TAIL n | TAIL PnL | entered/missed | avg delay |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | wait_le_02c | $+2537.24 | 26.18% | $+319.94 | 308 | $+1211.43 | 30 | $+1325.80 | 55/1 | 118.0m |
| 2 | immediate | $+1319.71 | 26.22% | $+382.77 | 309 | $+849.98 | 31 | $+469.72 | 56/0 | 0.0m |
| 3 | wait_le_03c | $+1319.71 | 26.22% | $+382.77 | 309 | $+849.98 | 31 | $+469.72 | 56/0 | 0.0m |
| 4 | wait_le_01c | $+197.30 | 41.69% | $-41.50 | 306 | $+276.27 | 28 | $-78.97 | 50/6 | 475.0m |

## Deployment Decision

Live TAIL keeps the signal band at YES ask `<= 0.03`, then blocks sizing and execution until the current YES ask is `<= 0.02`.
