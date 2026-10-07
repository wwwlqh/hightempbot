---
title: PENDING Ledger Pattern
type: concept
created: 2026-06-08
updated: 2026-06-08
status: stable
tags: [concept, ledger, persistence, safety, pending]
aliases: [PENDING row, pre-insert PENDING]
---

# PENDING Ledger Pattern

Before placing any order, `execution/pipeline.py` inserts a PENDING ledger row via `record_bet`. If the process crashes mid-submit, the PENDING row ensures there is always a record to reconcile against — no bet is silently lost.

## Lifecycle

```
record_bet(PENDING)  →  execute_or_log()  →  update_pending_bet_after_execution()
                                                  ├── BET (dry-run)
                                                  ├── FILLED (live, order confirmed)
                                                  └── CANCELLED (live, order rejected)
```

`update_pending_bet_after_execution` finalizes the row to `BET`, `FILLED`, or `CANCELLED` after the order attempt completes. A row that stays PENDING past `N` hours is treated as an orphan and reconciled on the next startup by `reconcile_orders`.

## Capital isolation

The pipeline counts PENDING rows toward `pending_exposure` before placing. This prevents double-placement when two ticks race on the same slot. Dry-run PENDING rows are excluded from live exposure.

## Schema sync invariant

Every field added to the bet dict MUST also be added to `COLUMNS` in `persistence/ledger.py`. Failure silently drops the column on insert. See [[invariants#schema-and-ledger]].

## See also

[[invariants#schema-and-ledger]] · [[Capital Snapshot]] · [[Strategy Config Registry]] · [[Edge-Preserving Sizing]]
