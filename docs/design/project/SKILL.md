---
name: hightempbot-design
description: Use this skill to generate well-branded interfaces and assets for HighTempBot, either for production or throwaway prototypes/mocks/etc. Contains essential design guidelines, colors, type, fonts, assets, and UI kit components for prototyping the live trading dashboard.
user-invocable: true
---

Read the README.md file within this skill, and explore the other available files.

If creating visual artifacts (slides, mocks, throwaway prototypes, etc), copy assets out and create static HTML files for the user to view. If working on production code, you can copy assets and read the rules here to become an expert in designing with this brand.

If the user invokes this skill without any other guidance, ask them what they want to build or design, ask some questions, and act as an expert designer who outputs HTML artifacts _or_ production code, depending on the need.

## At a glance

- **Surfaces**: warm cream `#faf9f6` page bg, white card `#ffffff`, hairline `#e8e5e0` borders. No drop shadows ever.
- **Type**: Calibri-first stack, Consolas mono. Tabular nums on every metric.
- **Color**: neutral by default, status colors (green/red/yellow/blue) only for state — always paired with a 6%-alpha tint.
- **Iconography**: none. Country flag emoji are the only decorative graphic.
- **Tone**: terse, lowercase-friendly, status-first ("STALE 18h"). No emoji except flags. No marketing voice.

## Files

- `README.md` — full design guidance (voice, surfaces, type, spacing, components)
- `colors_and_type.css` — all design tokens as CSS custom properties
- `ui_kits/dashboard_v2/data.js` — canonical `HTB_DATA` payload shape

The live dashboard itself is the reference implementation — read
`src/hightempbot/dashboard/static/v2/` (`styles.css`, `index.html`, and the
`.jsx` pages). The prototype twins that used to sit beside `data.js` were
removed on 2026-08-09 after drifting from the live SPA.
