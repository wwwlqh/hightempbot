# docs/

Working artifacts. Not authoritative.

**Authoritative sources for this project:**
- `AGENTS.md` (top-level) — agent workflow, trading invariants, code map
- `CLAUDE.md` (top-level) — deploy commands, server connection details
- `src/hightempbot/` — the live code
- [`../wiki/`](../wiki/README.md) — in-repo knowledge base (start at `wiki/README.md`; always read before exploring source)

**What lives here:**

| Folder | Purpose | Stable? |
|---|---|---|
| `runbooks/` | Operator runbooks (`live-operator-go-no-go.md`) | see the runbook's own status notes |
| `cleanup/` | Historical cleanup manifest (`redundant-files-2026-05-21.json`, `status: applied`) | historical only |
| `design/project/` | Design system reference (v2 dashboard) — `ui_kits/dashboard_v2/data.js`, `colors_and_type.css`, `README.md`, `SKILL.md` | `data.js` is referenced by `dashboard/app.py` and `dashboard/v2_data.py`; the live SPA in `dashboard/static/v2/` is the markup/CSS source of truth |

**What was removed on 2026-05-18:**
- `design/chats/`, `design/project/preview/`, `design/project/uploads/`, `design/project/ui_kits/dashboard/` (v1) — stale visualizations
- `validation/` (entire top-level folder) — pre-Candidate-#1 tuning artifacts
- 3 server-DB snapshot copies in `data/`

**What was removed on 2026-08-09:**
- `design/project/ui_kits/dashboard_v2/` — the 8 JSX/CSS/HTML prototype twins
  (`Calendar`, `Charts`, `Overview`, `Pages2`, `Pages3`, `Shell`, `styles.css`,
  `index.html`); they had drifted from `src/hightempbot/dashboard/static/v2/`
  and lacked newer payload keys. `data.js` is kept — it is still referenced by
  `dashboard/app.py` and `dashboard/v2_data.py`.
- `plans/2026-05-21-003-feat-live-operator-control-plan.html` — stale HTML twin
  of the (now `completed`) markdown plan.

**What was removed on 2026-10-07** (all recoverable from git history):
- `plans/` — repo-reorganization (completed), live-operator-control (completed),
  L2-champion src port (landed in `b07c13a`/`1cca3ff`), cross-tick top-up (shipped)
- `brainstorms/`, `audits/`, `rollout/` — the cross-tick top-up requirements,
  dashboard audit and rollout runbook, plus the 2026-05-23 redundancy audit
  (`status: applied`). New `plans/` etc. folders can be recreated as needed.

**Known tech debt (open items):**
- Legacy bracket-bound matching shims in `src/hightempbot/resolution/settler.py`
  (`_is_legacy_integer_bracket`, the legacy branches of `_bet_matches_winner`,
  `_continuous_bracket_bounds`). Deferred item D4 from the 2026-05-23 redundancy
  audit: the code comment says the compat path is removable once the
  `legacy-format compat` log line stops firing in production logs.

Per [authoritative_sources memory](../../../.claude/projects/c--Users-leowq-OneDrive-Desktop-hightempbot/memory/feedback_authoritative_sources.md): docs drift; do not rely on anything in this folder as ground truth for current code.
