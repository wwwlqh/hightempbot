---
type: decision
title: "Weekly Retention Policy"
created: 2026-05-04
updated: 2026-05-04
decision_date: 2026-05-04
status: active
tags:
  - decision
  - storage
  - housekeeping
  - operations
related:
  - "Scheduler Jobs"
  - "DB Schema"
  - "[[Oracle Cloud Server]]"
  - "FastAPI HTMX Dashboard"
---

# Weekly Retention Policy

On-disk growth on the [[Oracle Cloud Server]] is capped via uniform 7-day retention across the four high-churn surfaces — `signals`, `logs/hightempbot.log`, `market_tokens`, `pipeline_health`. Bets and ledger are **never pruned**; the historical bet record is preserved forever.

Commit `96ab930` (2026-05-04). Deployed to production same day; bot restarted clean (PID 110494, 148 jobs).

## Surfaces and mechanism

| Surface | Retention | Mechanism | Cadence |
|---|---|---|---|
| `signals` | 7 days | `DELETE FROM signals WHERE … created_at < datetime('now', '-7 days')` inside `scrape_actuals_job` | Per-station, daily after midnight actuals |
| `logs/hightempbot.log` | 7 days | `TimedRotatingFileHandler(when='midnight', backupCount=7, utc=True)` | Daily at UTC midnight |
| `market_tokens` | 7 days | `prune_market_tokens(retention_days=7)` with `NOT EXISTS` guard for PENDING bets | Weekly housekeeping |
| `pipeline_health` | 7 days | `prune_pipeline_health(retention_days=7)` | Weekly housekeeping |
| `bets` / `ledger` | **forever** | no prune anywhere | n/a |

## Cadence change: monthly → weekly

The housekeeping APScheduler entry was renamed and re-cron'd:

- **Before:** `_monthly_housekeeping_job`, `CronTrigger(day=1, hour=2, minute=0, timezone="UTC")`, id `monthly_housekeeping`
- **After:** `_weekly_housekeeping_job`, `CronTrigger(day_of_week="mon", hour=2, minute=0, timezone="UTC")`, id `weekly_housekeeping`

Without the cadence change, a 7-day TTL pruned monthly would let `pipeline_health` grow to ~1M+ rows before each sweep (~36K rows/day × 30 days), defeating the cap. Weekly cadence keeps it bounded at ~252K rows.

## Rationale

The original concern was Oracle Cloud Always Free 6 GB RAM tier — but storage is a separate budget (default ~46.6 GB boot volume, resizable to 200 GB free). Code-level estimation showed:

- DB tables grow ~0.5 GB/yr — non-issue for a decade
- `logs/hightempbot.log` was the actual ticking clock: plain `FileHandler` with no rotation could reach 2–18 GB/yr (5–50 MB/day worst case)

7-day rolling retention caps the log file at ~35–350 MB worst case, which removes the failure mode entirely. The DB tables are tightened as a defense-in-depth pass since they share the same housekeeping job.

## Why these surfaces and not others

- **`forecast_archive`** — also unbounded (~300–500 MB/yr), but slow grower, useful for retrains. Out of scope for this change; revisit when it crosses 5 GB.
- **`calibration_params_history`** — ~50 MB/yr, walk-forward training history. Kept for diagnostics.
- **`bets`/`ledger`** — explicitly preserved forever per operator intent. PnL audit trail.

## Constraints honored

- **PENDING bet protection** — `prune_market_tokens` retains the `NOT EXISTS (SELECT 1 FROM ledger l WHERE … outcome = 'PENDING' AND event_type IN ('bet','dry_run'))` guard. A 7-day-old token whose market still has an open PENDING bet survives.
- **Per-station signals dependence** — the `signals` 7-day prune fires inside `scrape_actuals_job`, so a station whose midnight actuals scrape fails for >7 days will accumulate signals until the next successful run. Pre-existing weakness; not addressed in this change.
- **In-memory APScheduler jobstore** — the `monthly_housekeeping` → `weekly_housekeeping` job-id rename is safe because no jobstore is configured (default in-memory) and `schedule_all_jobs` removes all jobs on boot before re-registering.

## Files changed (commit `96ab930`)

- `src/hightempbot/scheduler/jobs.py` — signals retention `-3` → `-7` days; housekeeping job renamed + cron monthly → weekly
- `src/hightempbot/main.py` — `FileHandler` → `TimedRotatingFileHandler`; added `import logging.handlers`
- `src/hightempbot/persistence/ledger.py` — `prune_market_tokens` default `3` → `7` (file moved from `execution/` to `persistence/` in 2026-05-19 reorg)
- `src/hightempbot/dashboard/app.py` — docstring retention update
- `tests/test_scheduler.py` — `test_monthly_housekeeping_job_exists` → `test_weekly_housekeeping_job_exists`

387/387 tests pass after the change.

## See also

Scheduler Jobs · DB Schema · [[Oracle Cloud Server]] · FastAPI HTMX Dashboard
