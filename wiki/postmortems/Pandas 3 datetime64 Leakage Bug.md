---
type: postmortem
title: "Pandas 3 datetime64 Leakage Bug"
created: 2026-05-08
updated: 2026-05-08
incident_date: 2026-05-08
status: resolved
severity: high
tags:
  - postmortem
  - backtest
  - leakage
  - pandas
  - reproducibility
related:
  - "[[Backtest Harness]]"
  - "Live-Match Evaluator"
  - "[[Decision Table]]"
---

# Pandas 3 datetime64 Leakage Bug

The per-hour readiness gate in the [[Backtest Harness|backtest harness]] silently collapsed when run under pandas 3.x, nearly doubling the bet stream and inflating BR100 test PnL from the correct **$516.55** to **$1138–$1495**. Discovered 2026-05-08 while investigating why ce-optimize "champions" did not reproduce in the user's PowerShell terminal.

## What happened

Two environments running the *exact same* `decision_table.parquet`, `polymarket_history.db`, and `tp_sl_config.json` produced different bet counts and different PnL:

| Environment | Python | pandas | BR100 train PnL | BR100 test PnL | bets train/test |
|---|---|---|---:|---:|---:|
| User PowerShell, `python.exe` (system) | 3.13.12 | **2.3.3** | +$124.52 | +$516.55 | 301 / 293 |
| Agent Bash, `.venv\Scripts\python.exe` | 3.13.12 | **3.0.2** | +$190.25 | +$865.94 | 434 / 611 |

Same scripts, same data — different bet stream. File hashes (sha256) of parquet, db, and config were byte-identical across both environments. Divergence was entirely inside pandas.

## Root cause

`backtest/live_match_eval.py:349` (and the same pattern at `backtest/sweep_lib.py:641`) converted `market_date` to Unix seconds via:

```python
md_unix = pd.to_datetime(df["market_date"]).astype("int64").to_numpy() // 10**9
```

This relies on the default `datetime64` unit being **nanoseconds**:

| pandas | default datetime64 unit | int64 of `2026-02-18` | `// 10**9` |
|---|---|---:|---:|
| 2.x and earlier | `[ns]` | 1 771 372 800 000 000 000 | 1 771 372 800 ✓ Unix seconds |
| **3.x** | `[us]` | 1 771 372 800 000 000 | **1 771 372 ✗ ~1000× too small** |

The downstream `entry_ts >= md_unix` filter was supposed to reject candidates whose snapshot timestamp was earlier than `market_date 00:00 UTC`. Under pandas 3, `md_unix` was so small that almost every `entry_ts` passed the comparison. The leakage gate became vacuous.

Effect: pre-`market_date` snapshots leaked into the candidate stream, ~doubling bet counts and inflating PnL with bets that never could have fired in live execution.

## Symptoms

- ce-optimize "wins" (e.g. NO `0.06/0.25/fp0.71` + TAIL `fp_max=0.04` showed BR100 test +$1495 in agent harness) did not reproduce in user's PowerShell (+$598 there)
- Agent harness reported ~1.5× more bet candidates than user's PowerShell on the same parquet
- Train PnL diverged in a config-dependent way (1.5–2.0× ratio agent/user)

## Fix

Cast to `datetime64[s]` before `int64`. The resulting integer is then guaranteed to be Unix seconds regardless of pandas version:

```python
md_unix = pd.to_datetime(df["market_date"]).astype("datetime64[s]").astype("int64").to_numpy()
```

Applied at:
- `backtest/live_match_eval.py:349` (the runtime leakage gate read by `measure_tp_sl.py`)
- `backtest/sweep_lib.py:641` (the parquet-build leakage gate in `build_decision_table.py`)

After the fix, both pandas 2.3.3 and pandas 3.0.2 produce **byte-identical** measurements:

```
train_pnl = +$124.5205
test_pnl  = +$516.5483
train_dd  =  33.7395%
test_dd   =  21.7686%
n_train   =  301
n_test    =  293
```

## Verification

`measure_tp_sl.py` was run under both Python installs after the fix; output JSON matched to 4 decimals across all gate and diagnostic fields. Diagnostic script at `.context/compound-engineering/ce-optimize/all-bankroll-pnl-v3/diagnose.py` fingerprints config/parquet/db sha256, candidate stream sha256, library versions, and end-to-end PnL — useful for future cross-environment audits.

## Impact on prior optimization

All ce-optimize results recorded under `optimize/all-bankroll-pnl-v2` (and earlier rounds run on `.venv` pandas 3) are tainted: PnL was inflated by the leakage. The ranking signal those rounds used is unreliable. Branch was archived with stale-marker commits (see `7c00d2b`, `984c9ed`).

The pre-leak `optimize/all-bankroll-pnl` baseline (NO `0.075/0.20/fp0.70 + h=[0..6] + size 0.05`; TAIL alpha 3.5 4-of-4 fp 0.001-0.05 size 0.015 tp 0.20) was correct under both pandas versions because it had been validated against pandas 2 by the user. That config is restored as the production champion on `optimize/all-bankroll-pnl-v3`.

## Lessons

1. **Library defaults are an implicit contract.** `astype("int64")` on a datetime column hides the unit. A single explicit `astype("datetime64[s]")` would have made the code version-independent.
2. **Cross-version reproducibility checks belong in the harness.** A 5-line "diagnostic" script that fingerprints inputs and re-runs the harness would have surfaced this in minutes instead of through hours of mismatched results.
3. **Trust the user's PowerShell environment as ground truth.** When the agent's measurements disagreed with PowerShell, the agent should have suspected its own environment first.
4. **`venv` pandas was upgraded to 3.0.2 without verification.** Pinning major versions in `pyproject.toml`/`requirements.txt` and checking that the pinned version matches the system Python that PowerShell uses would prevent the same divergence happening again.

## Files changed

- `backtest/live_match_eval.py` — datetime64[s] cast (commit `25cc373`)
- `backtest/sweep_lib.py` — same fix (commit `3dc93ea`)
- `backtest/tp_sl_config.json` — reverted to parity baseline alongside the fix (commit `25cc373`)

> [!note] 2026-05-09 — further parity fix changed the post-fix baseline
> The +$516.55 figure verified above was the correct BR$100 test PnL after the pandas-only fix on 2026-05-08. On 2026-05-09 a separate parity audit found two more divergences between backtest and live: an EMOS sigma-floor mismatch (0.5°C vs 0.1°C) and a bracket-bound parsing mismatch (extends-to-next-edge vs ROUND-rule midpoints). The bracket-parser fix alone shifted BR$100 train PnL by ~$118 and test PnL up to **+$624.11** (under the prior NO+TAIL config). Three-way diagnostic showed bracket parser drove 98% of the BR$100 train shift; sigma floor only 2%. The +$516.55 figure is therefore an intermediate (pandas-only) state, not the final correct baseline. See [[Parity Report: src vs backtest (2026-05-09)]] and [[Optimum Strategy]] for the post-parity Candidate #1 strategy.

## See also

[[Backtest Harness]] · Live-Match Evaluator · [[Decision Table]] · [[Optimum Strategy]] · [[Parity Report: src vs backtest (2026-05-09)]]
