# ML Dataset — Stage 1 (leak-free assembly)

Purpose: for each traded station, assemble (a) its own multi-centre ensemble
forecast history, (b) its actual daily highs (ground-truth labels), and (c) daily
max temps at 8 surrounding grid points + the target point ("neighbor ring"), so
Stage 2 can train models predicting the target's **next-day Tmax distribution**
from `[own ensemble + neighbor obs through day D-1]`.

Everything temperature-valued is stored in **degrees Celsius**.

Assembled 2026-07-17.

## Station list (46)

Source of truth for membership: the distinct `station_id` values in
`backtest/data/decision_table_may11plus_l2.parquet` (46 stations — more than the
~27 the task anticipated). Lat/lon/timezone/unit come from the server's
`enrolled_stations` table (all 46 are `status='LIVE'` with valid coordinates;
`src/hightempbot/stations.py` holds no hardcoded coords — the registry is
DB-driven). See `stations.csv`.

CYYZ DNMM EDDM EFHK EGLC EHAM EPWA FACT KATL KAUS KBKF KDAL KHOU KLAX KLGA KMIA
KORD KSEA KSFO LEMD LFPG LIMC LTAC MMMX MPMG NZWN OEJN OPKC RJTT RKPK RKSI RPLL
SAEZ SBGR VILK WIHH WMKK WSSS ZBAA ZGGG ZGSZ ZHHH ZSPD ZSQD ZUCK ZUUU

## Files

| file | rows | grain |
|------|------|-------|
| `stations.csv` | 46 | one row per station |
| `forecasts.parquet` | 71,144 | (station, target_date, centre) — deduped |
| `actuals.parquet` | 85,613 | (station, local_date) |
| `neighbors.parquet` | NEIGHBOR_ROWS | (station, point_label, date) |

Intermediate/raw artifacts kept for provenance: `forecast_2026-0{2..7}.csv`
(raw per-month pulls, pre-dedupe), `actuals_all.csv`, `enrolled_stations_all.csv`
(full 52-row enrolled table incl. stations not in scope), `neighbor_gaps.csv`,
`neighbors_fetch.py` (the fetch script), `station_ids.txt`.

---

## stations.csv

Columns: `station_id, city, lat, lon, tz, unit, resolution_source`

- `lat`/`lon`: WGS84 decimal degrees (station airport location).
- `tz`: IANA timezone (e.g. `America/New_York`) — the station's local day, which
  defines both the Polymarket market day and the `actuals.local_date`.
- `unit`: the **Polymarket resolution/bracket unit** — `F` for the 11 US
  stations, `C` for the other 35. THIS IS METADATA ONLY. It describes how the
  market's temperature brackets are labeled; it does **not** describe the units in
  these parquets. All stored temperatures (forecast, actual, neighbor) are °C.
- `resolution_source`: how actuals are scraped (all 46 in scope are `wu` =
  Weather Underground).

Unit split: 35 × `C`, 11 × `F` (KATL KAUS KBKF KDAL KHOU KLAX KLGA KMIA KORD
KSEA KSFO).

---

## forecasts.parquet

The station's own ensemble forecast history, **horizon = 1 only** (forecast for
`target_date` issued on `issue_date = target_date − 1 day`). Horizon=1 is the
leak-safe next-day forecast: it is available before the target day begins.

Columns: `station_id, target_date, horizon, issue_date, centre, member,
tmax_celsius, ingested_at`

- `target_date` (str `YYYY-MM-DD`): the local day the forecast is for (= label day).
- `issue_date`: always `target_date − 1`.
- `centre` (str): the forecast provider = the **canonical ensemble-member
  identity**. Key Stage-2 features on `centre`, NOT on `member` (see caveat).
- `member` (int): the bot's internal member index. **Not stable** — see caveat.
- `tmax_celsius` (float): forecast daily high, °C.
- `ingested_at` (str): when the bot pulled the row (used for dedupe).

### Coverage
- Date range: `target_date` **2026-02-01 → 2026-07-16** (166 consecutive days).
- **Every one of the 46 stations has all 166 days** — no missing forecast days.
- `horizon` is always 1 (the source table only stores h=1 for these).
- Requested window was Feb→Jul 2026; the source table also holds h=1 history back
  to **2021-01-01** for these stations (~546k rows) if Stage 2 wants more training
  data — re-pull with a wider `target_date` bound.

### Ensemble structure — 9 or 10 centres, and the member caveat
The 10 centres that appear in-window and their *typical* member index:

    ecmwf_ifs025=1  gfs_seamless=2  icon_seamless=3  gem_seamless=4
    meteofrance_seamless=5  ukmo_seamless=6  knmi_seamless=7  dmi_seamless=8
    ncep_gfs013=9   (+ jma_seamless, see below)

- The core 5 (ecmwf, gfs, icon, gem, meteofrance) are present for **all** 166
  station-days at fixed indices 1–5.
- **jma_seamless** was part of the ensemble **2026-02-01 → 2026-04-15** only
  (member=6), then dropped on 2026-04-16. While jma was present the ensemble had
  **10 members** and ukmo/knmi/dmi/ncep were pushed to indices 7/8/9/10; after jma
  was dropped they sit at 6/7/8/9 (**9 members**).
- Post-dedupe centres-per-station-day: **9 centres × 5,216 station-days** and
  **10 centres × 2,420 station-days**.
- **7 stations never received jma at all** (tropical/subtropical, outside JMA's
  useful domain) and are always 9 centres: DNMM FACT OEJN OPKC RPLL WIHH ZSQD.
- CONSEQUENCE / LEAK-NOTE: because indices shift across the Apr-15/16 transition,
  the integer `member` is ambiguous (e.g. member 6 = jma before Apr 16, ukmo
  after). **Use `centre` as the member key.** `member` is retained only as raw
  provenance.

### Dedupe rule — APPLIED
The project dedupe rule (memory `lut_dedupe_fix`) was applied when building this
parquet: for each `(station_id, target_date, centre)`, keep the row with the
**latest `ingested_at`**. This collapses re-ingestions (the same station-day
re-fetched under both the 9- and 10-member numbering schemes at different times).

- Raw pulled rows (Feb–Jul, h=1): 74,711.
- Removed `live_deterministic` (27 rows): the live bot's own single deterministic
  run, not an NWP ensemble member, and its `member=1` collides with ecmwf.
- Dedupe on `(station, target_date, centre)` latest `ingested_at`:
  74,684 → **71,144** (dropped 3,540 re-ingestion duplicates).
- The raw per-month CSVs (`forecast_2026-0*.csv`) are pre-dedupe if you need to
  re-derive.

---

## actuals.parquet

Ground-truth daily highs = **training labels**.

Columns: `station_id, local_date, tmax_celsius, source, ingested_at`

- `local_date` (str `YYYY-MM-DD`): the station-local calendar day.
- `tmax_celsius` (float): observed daily high, °C.
- `source`: all `wu` (Weather Underground) — the only authoritative actuals source
  (project rule `wu_only_source`).
- Date range **2021-01-01 → 2026-07-16**; 46 stations; no duplicate
  `(station, local_date)` rows.
- 2026 coverage (Jan 1 → Jul 16 = 197 possible days): most stations 193–197 days;
  a few have minor gaps — lowest are **DNMM 184**, **MPMG 190**. All others ≥193.

---

## neighbors.parquet

Independent observation context around each target: daily Tmax at the target point
and 8 surrounding points, from the **Open-Meteo Archive (ERA5 reanalysis)** API
(free, no key). This is a *reanalysis observation* feature source, distinct from
the WU actuals label and from the bot ensemble.

Columns: `station_id, point_label, lat, lon, date, tmax_c`

- `point_label`: `TGT` (the station's own grid cell) plus `N NE E SE S SW W NW`.
- `lat`/`lon`: the **ERA5 grid-snapped** coordinates actually returned by the API
  (true data location), not the exact requested point.
- `date` (str `YYYY-MM-DD`): local calendar day (`timezone=auto` per point).
- `tmax_c` (float): daily max 2m temperature, °C.

### Ring geometry
For each station, 8 points at bearings N/NE/E/SE/S/SW/W/NW at ~100 km radius:
`dlat = 0.9·cos(θ)`, `dlon = 0.9·sin(θ)/cos(lat)` (θ measured clockwise from
north; 0.9° ≈ 100 km). Longitude wrap and pole clipping handled (no in-scope
station is near either edge). One API request per station (9 comma-separated
coords → a 9-element location list, returned in input order), sequential ~1 req/s,
one retry on failure.

### Coverage
- Requested `start_date=2026-01-01`, `end_date=2026-07-15`.
- ERA5 has ~5-day lag but coverage is **complete through 2026-07-15** (196 days
  per point, no trailing-lag gap for this window).
- Rows: NEIGHBOR_ROWS  (= 46 stations × 9 points × ~196 days).
- Null `tmax_c` values: NEIGHBOR_NULLS.  Recorded gaps: see `neighbor_gaps.csv`
  (NEIGHBOR_GAPS entries).

NOTE (project rule `om_historical_invalid`): the Open-Meteo **Historical
Forecast** API is deterministic-only and invalid as ensemble members — it was NOT
used. Only the **Archive/ERA5** endpoint was used here, and only for the neighbor
observation feature (never as an ensemble member).

---

## °C / °F notes

- `forecast_archive.tmax_celsius` and `actuals.tmax_celsius` are already °C in the
  source DB — copied through unchanged.
- Open-Meteo Archive returns °C by default (no `temperature_unit` override) —
  stored as-is.
- No Fahrenheit conversion was performed on any stored temperature. The `unit`
  column in `stations.csv` (F for 11 US stations) refers only to how Polymarket
  *labels its brackets*; it does not touch the numeric temperatures here.

## Leak-safety summary (for Stage 2)

- Forecasts are `horizon=1` only (issued day D−1 for target day D) → safe as a
  next-day predictor feature.
- Neighbor obs for day D is same-day reanalysis; to predict day D's target Tmax
  **use neighbor values only through D−1** (the task's "neighbor obs through
  day D−1"). Same-day neighbor Tmax would leak.
- Labels = `actuals.tmax_celsius` (WU). The neighbor `TGT` series is ERA5, a
  *different* estimate of the same quantity — a feature, not the label.
- Join keys: forecasts `target_date` ↔ actuals `local_date` ↔ neighbors `date`
  (all station-local `YYYY-MM-DD`).

## Coverage gaps / caveats
- 7 stations always 9-centre (never jma): DNMM FACT OEJN OPKC RPLL WIHH ZSQD.
- Actuals 2026 minor day gaps (DNMM 184, MPMG 190; others ≥193 of 197).
- `member` integer is not stable across the 2026-04-15/16 jma transition — key on
  `centre`.
- Neighbor lat/lon are ERA5-grid-snapped; nearby stations can share a grid cell.
- Neighbor-specific gaps: see `neighbor_gaps.csv`.
