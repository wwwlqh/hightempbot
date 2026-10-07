# HighTempBot Design System

A design system for **HighTempBot** — an automated Polymarket temperature-trading bot with a live operations dashboard. The dashboard is the only user-facing surface: a single-operator cockpit for tracking the bot's bets, calibration, station eligibility funnel, and P&L.

## Sources

The live v2 dashboard SPA is the single source of truth for markup and CSS.
This design system documents its conventions; it does not mirror its files.

- **Codebase** (read-only mount): `src/hightempbot/`
  - `dashboard/static/v2/styles.css` — full token + component CSS
  - `dashboard/static/v2/index.html` — SPA entry point (shell, nav, mount)
  - `dashboard/static/v2/Shell.jsx` — top-level shell, sticky nav, footer
  - `dashboard/static/v2/Overview.jsx` — Overview KPIs + funnel
  - `dashboard/static/v2/Pages2.jsx` — eligibility-stage / stations tables
  - `dashboard/static/v2/Pages3.jsx` — station detail, LUT bucket drilldown
  - `dashboard/static/v2/Charts.jsx` — equity curve, heatmap, calibration
  - `dashboard/static/v2/Calendar.jsx` — day-grid P&L calendar
  - `dashboard/static/v2/Strategy.jsx` — per-strategy cards and gates
  - `dashboard/static/v2/Operator.jsx` — Stop/Start + transfer controls
  - `dashboard/app.py` — FastAPI backend serving the SPA and JSON payloads
  - `dashboard/v2_data.py` — builds `HTB_DATA`, the payload the SPA renders

## Product context

HighTempBot trades Polymarket "will tomorrow's high temperature be ≥ X°" markets across ~30+ international weather stations. The pipeline is:

```
ingestion (forecasts + actuals)  →  calibration (EMOS + LUT bucket stats)
   →  decision (edge gates)  →  execution (Polymarket order)  →  resolution (P&L)
```

The dashboard is **operations-centric**, not investor-facing. It prioritizes:

1. **Pipeline funnel** — Enrolled → WU source → Coverage → Active → Bettable
2. **Per-station forensics** — LUT freshness, bucket counts, EMOS μ/σ, forecast model spread
3. **P&L surface** — equity curve, by-strategy (NO/YMID/TAIL), station × strategy heatmap, resolved ledger
4. **Risk** — drawdown bar, halted-banner, dry-run badge

## Index

- `README.md` — this file
- `colors_and_type.css` — full design tokens (CSS custom properties)
- `SKILL.md` — Agent Skill manifest for cross-environment use
- `ui_kits/dashboard_v2/data.js` — canonical `HTB_DATA` shape reference,
  mirrored by `dashboard/v2_data.py::build_htb_data`

There is no icon/asset directory: the system ships no custom icon set (see
Iconography). The JSX/CSS prototype twins that once lived beside `data.js`
were removed on 2026-08-09 — they had drifted from the live SPA and lacked
newer payload keys. Read `src/hightempbot/dashboard/static/v2/` instead.

## Content fundamentals

The bot speaks like an **operator's terminal log**: terse, lowercase-friendly, status-first. Voice is third-person about the bot, never "we" or "I". Numbers carry their full precision, units are always present (`%`, `°C`, `°F`, `pp`, `$`, `h`).

**Voice & tone**

- **Terse, typographic, declarative.** No marketing language. No exclamation marks. No emoji except country flag emoji (used as ICAO-derived prefixes — "🇯🇵 RJTT").
- **Status before noun.** "STALE 18h" beats "18 hours stale".
- **Pipeline metaphors are explicit.** "EMOS → LUT → market", "edge gate", "buffer-zero cutover".
- **Casing**: Sentence case for headings ("Resolved bets", "By strategy"). UPPER for status pills (`LIVE`, `DRY_RUN`, `WIN`, `LOSS`, `STALE`, `HALTED`). lowercase mono for code-like values (`ymid_tp`, `lut_bucket_stats`).
- **No "you".** The interface speaks past the operator, not at them. Tooltips describe mechanism: "Distinct resolved station-days seeded into LUT history" — not "Click to learn more".

**Examples lifted from the codebase**

- Hero desc: "All enrolled Polymarket stations, grouped by live eligibility stage."
- Cold-start state: "No `lut_bucket_stats` rows yet. This station either hasn't resolved enough actuals to seed the LUT, or its walk-forward EMOS refit is still running."
- Halted banner: "REDUCED SIZE — Drawdown 4.2% reached 5%. Betting at half size until recovery."
- Tooltip on a YMID exit card: "per SL exit (negative is the design)"

**Typographic conventions**

- `→` (U+2192) for pipeline flow, `›` (U+203A) for funnel arrows, `&middot;` for inline separators, `&mdash;` for empty/missing values.
- Numbers always tabular: `font-variant-numeric: tabular-nums` on every metric.
- Edge displayed as percentage points with sign: `+4pp`, `-2pp`. Probabilities as percent: `52%`. P&L as signed dollars: `+$12.34`, `-$5.10`.
- Time stamps in mono, formatted `MM-DD HH:MM` in Malaysia Time (operator's TZ).

## Visual foundations

**Surface system.** Warm off-white background (`#faf9f6` — feels like aged paper, *not* pure white) on top of pure-white cards (`#ffffff`). Borders are a single hairline (`#e8e5e0`). No drop shadows. No glassmorphism. The sticky top nav uses `backdrop-filter: blur(10px)` over a 95%-opacity tint of the page bg — the only place blur is used.

**Color philosophy.** Neutral by default, color *only* for status. Greens / reds / yellows / blues are pulled from a Tailwind-adjacent palette (`#16a34a`, `#dc2626`, `#ca8a04`, `#2563eb`) and are always paired with a 6%-alpha background tint when used as a pill/cell. Nothing is decoratively colored — if you see green, something resolved or won.

**Type.** Calibri-first stack with Segoe UI / system fallbacks. Display and UI are the same family — no Serif/sans split. Mono is Consolas-first. Tracking is tightened on display weights (`-0.02em` on h1, KPI numbers). Labels are uppercase 0.72em with `letter-spacing: 0.1em`.

**Spacing.** A simple 8 / 16 / 24 scale (`--gap-sm`, `--gap`, `--gap-lg`). Card internal padding is `16-24px`. Dense tables use `9-12px` row padding. The page is capped at `1200px` and centered.

**Backgrounds.** Solid colors only. **No gradients**, no images, no patterns, no textures. The only animation tied to a background is `pulse-red` on the halted banner (2s opacity oscillation).

**Borders & radii.** Hairline `1px solid var(--border)` everywhere. Three radii only: `8px` (cards), `5px` (small panels), `3px` (chips, inputs). 999px pill radius for status pills.

**Shadows.** *None* on resting state. Cards have border + flat fill, period. Hover state on cards swaps `border-color` from `--border` (#e8e5e0) to `--text-muted` (#999) — never raises with a shadow.

**Hover & press.**
- Nav links / buttons: change `color` → `--text` and `background` → `rgba(0,0,0,0.03)`.
- Table rows: `background: var(--hover)` (rgba 0,0,0,0.02) on hover.
- Sortable headers: `color` darkens to `--text` on hover.
- Buttons in the segmented control get `background: var(--text)` and `color: var(--bg)` when active — high-contrast inversion.
- No scale/shrink animations. The only `transform: scale()` is on BSS heatmap cells (`scale(1.3)` on hover).

**Transitions.** All durations are 0.15s with default easing. Properties transitioned: `color`, `background`, `border-color`, `opacity`. No `cubic-bezier`, no spring animations, nothing exotic.

**Transparency & blur.** Used in two places only: (1) the sticky nav (`backdrop-filter: blur(10px)`); (2) status-color background tints (`rgba(*, 0.06)`). Otherwise everything is opaque.

**Imagery vibe.** None. The product has no photography, no illustration. The only "imagery" is country flag emoji injected per-station via ICAO-prefix derivation. There are no logos beyond the wordmark "HighTempBot".

**Layout rules.** Sticky top nav (56px tall). Content centered at 1200px max. Tables go full-width inside their card. Two-column at desktop, single column at <768px. KPI grids use `repeat(auto-fit, minmax(170px, 1fr))`.

**Cards.** White fill, `1px` border, `8px` radius, 16-18px internal padding. No shadow. The only decorative card variant is `.position-card` which adds a 3px-thick colored left border (green for win, red for loss) — used sparingly on resolved positions.

## Iconography

The codebase **does not ship a custom icon set**. There is no SVG sprite, no icon font, no Lucide/Heroicons import. The visual vocabulary is built almost entirely from typography + colored dots/pills.

What is used in lieu of icons:

- **Country flag emoji** — generated dynamically from ICAO prefix → ISO country code → regional indicator characters (see `_icao_to_flag` in `app.py`). Every station row leads with one (🇺🇸 KJFK, 🇯🇵 RJTT, 🇩🇪 EDDF…).
- **Colored dots** (`.health-dot`, `.station-dot`, `.stepper-dot`) — `8px` or `14px` solid circles. Status: green/yellow/red/muted.
- **Colored squares** (`.g`, `.bss-cell`) — `12-16px` rounded squares for gate/heatmap cells.
- **Unicode glyphs**:
  - `▶` / `▼` (U+25B6 / U+25BC) — sortable-table sort direction
  - `▶` / `▼` — accordion expand/collapse on station rows
  - `›` (U+203A) — funnel arrow between stages
  - `→` (U+2192) — pipeline flow ("EMOS → LUT → market")
  - `&middot;` — inline separator
- **Pills** (`.pill .pill-green` etc.) — uppercase 0.74em text in a colored capsule.

**Approach for new designs.** Continue the no-iconography stance. If a real icon is unavoidable (e.g. a settings gear, a search magnifier), reach for **Lucide** (`https://unpkg.com/lucide@latest`) at 16-18px stroke-1.5 — its restraint matches the system best. Document any addition here. Flag emoji are the only "decorative" graphic we use — keep that exclusive.
