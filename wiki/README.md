---
title: HighTempBot Wiki
type: meta
created: 2026-05-26
updated: 2026-06-08
tags: [meta, readme, orientation]
status: stable
---

# HighTempBot — Agent Orientation

You are an agent (Claude / Codex / other) reading this to get oriented in the [HighTempBot](..) codebase. This wiki is **not** a code mirror — `src/` is the source of truth for behavior. The wiki carries what you cannot infer from code alone.

## Read these first, in order

1. **[[invariants]]** — rules that look optional but aren't. Violating these silently breaks calibration or capital safety.
2. **[[glossary]]** — domain terms (`p_E`, `p_L_loose`, TAIL, BR100, fp_min, etc.) used across code, memory, and Polymarket.
3. **[[map]]** — pointer-only index. "For X, read `src/path/foo.py`." No semantics.
4. **[[Optimum Strategy]]** — the active production spec. Timestamped. The only page authoritative on current strategy parameters.

After those four, drill down only as the task demands.

## When to read what

| Task | Read |
|---|---|
| "What does the current strategy do?" | [[Optimum Strategy]] |
| "Can I change X without breaking calibration?" | [[invariants]] |
| "What does `p_L_loose` mean?" | [[glossary]] |
| "Where does bracket math live?" | [[map]] |
| "Why was EMOS-only reverted?" | `decisions/` |
| "What broke on 2026-05-12 parity audit?" | `postmortems/` |

## Hard rules

- **The codebase is ground truth.** If the wiki and `src/` disagree, `src/` wins and the wiki is stale. Fix the wiki, not the code.
- **`docs/` (runbooks, design, any future plans) is NOT authoritative.** They drift faster than the wiki. Treat as historical context only.
- **Per-bet display fields stay row-exact** — never aggregate `fill_price`, `fill_size`, `realized_edge` for the dashboard. See [[invariants#dashboard]].

## Layout

```
hightempbot/
  README.md            ← you are here
  invariants.md        ← non-negotiable rules
  glossary.md          ← terms
  map.md               ← src/ pointer index
  concepts/            ← load-bearing concepts (backtest harness, EMOS, LUT, signal flavors)
  decisions/           ← why-arcs, one per significant choice
  postmortems/         ← incidents
  entities/            ← external systems (WU, Open-Meteo, Polymarket, server)
  sources/             ← project + module summaries
```
