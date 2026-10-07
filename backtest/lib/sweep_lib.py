"""Reusable harness for backtest strategy exploration.

Pieces:
- bracket parser (single °C/°F, range "between X-Y°F", tail "X or below/higher")
- walk-forward EMOS loader (calibration_params_history, asof_date <= market_date)
- walk-forward LUT builder (pred_bucket_history with local_date < market_date)
- 9 signal flavors as columns
- entry-time price loader (latest snapshot >= 4h before bracket close UTC)
- leakage asserts (per-row, raise on violation)
- evaluate_config(parquet_df, config) -> metrics  (the function ce:optimize calls)

Used by build_decision_table.py (Phase 1) and eval_strategy.py (search loop).
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytz
from scipy.stats import norm

from backtest.lib import honest_report as hr

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIVE_DB = REPO_ROOT / "data" / "hightempbot_server_latest.db"
MARKET_DB = REPO_ROOT / "backtest" / "data" / "polymarket_history.db"
PARQUET_OUT = Path(os.environ.get(
    "HTB_DECISION_TABLE",
    REPO_ROOT / "backtest" / "data" / "decision_table_may11plus.parquet",
))

FEE_K = 0.05

# Local-hour candidates for time-of-day exploration (in each station's local TZ).
# All respect the 4h-before-close margin (bracket close = local 24:00).
# Full 24h sweep enabled 2026-05-06 to find the precise best hour.
ENTRY_LOCAL_HOURS = tuple(range(24))

EXPECTED_MODELS = (
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
    "gem_seamless",
    "meteofrance_seamless",
    "ukmo_seamless",
    "knmi_seamless",
    "dmi_seamless",
    "ncep_gfs013",
)


# --------------------------------------------------------------------------- bracket parser

_RX_SINGLE = re.compile(
    r"^(?P<sign>-?)(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[CF])\s*(?P<tail>or\s+below|or\s+higher)?$",
    re.IGNORECASE,
)
_RX_RANGE = re.compile(
    r"^between\s+(?P<lo>-?\d+(?:\.\d+)?)\s*[-–]\s*(?P<hi>-?\d+(?:\.\d+)?)\s*(?P<unit>[CF])$",
    re.IGNORECASE,
)


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def parse_bracket(label: str) -> tuple[float, float, str] | None:
    """Parse a Polymarket bracket label into (lo_C, hi_C, kind).

    Uses ROUND-rule semantics matching the live parser
    (`src/hightempbot/resolution/gamma.py::parse_bracket_bounds`):
    a label "X" represents actual ∈ [X-0.5, X+0.5) before unit conversion. This
    matches Polymarket's resolution mechanic of rounding the reported integer
    actual to the nearest bracket label.

    Returns (lo, hi, kind) where bounds are in Celsius and kind ∈ {'low','mid','high'}.
    None if unparseable.

    Was "extends-to-next-bracket-lower-edge" semantics (val + 1.0 / val + 2.0) until
    2026-05-09 — produced the same `kind` classification but bounds shifted by
    +0.5°F at every edge vs live. Aligned to live 2026-05-09 after the parity audit.
    See wiki/meta/parity-report-src-vs-backtest for the analysis.
    """
    if not label:
        return None
    s = re.sub(r"[°˚℃℉]", "", label).strip()

    m = _RX_RANGE.match(s)
    if m:
        lo_v = float(m.group("lo"))
        hi_v = float(m.group("hi"))
        unit = m.group("unit").upper()
        # ROUND rule: range "X-Y" covers labels {X, X+1, ..., Y}, so the
        # continuous actual range is [X - 0.5, Y + 0.5) before unit conversion.
        if unit == "F":
            return _f_to_c(lo_v - 0.5), _f_to_c(hi_v + 0.5), "mid"
        return lo_v - 0.5, hi_v + 0.5, "mid"

    m = _RX_SINGLE.match(s)
    if not m:
        return None
    val = float(m.group("num"))
    if m.group("sign") == "-":
        val = -val
    unit = m.group("unit").upper()
    tail = (m.group("tail") or "").lower().strip()

    if "below" in tail:
        # ROUND rule: "X or below" covers labels {..., X-1, X}, continuous
        # actual range is (-inf, X + 0.5) before unit conversion.
        upper_tail_c = _f_to_c(val + 0.5) if unit == "F" else val + 0.5
        return -math.inf, upper_tail_c, "low"
    if "higher" in tail:
        # ROUND rule: "X or higher" covers labels {X, X+1, ...}, continuous
        # actual range is [X - 0.5, +inf) before unit conversion.
        lower_tail_c = _f_to_c(val - 0.5) if unit == "F" else val - 0.5
        return lower_tail_c, math.inf, "high"

    # Single mid bracket: "X" → actual ∈ [X - 0.5, X + 0.5) before unit conversion.
    # Live's `be X on date` pattern. 1-unit-wide regardless of F/C.
    if unit == "F":
        lo_c, hi_c = _f_to_c(val - 0.5), _f_to_c(val + 0.5)
    else:
        lo_c, hi_c = val - 0.5, val + 0.5
    return lo_c, hi_c, "mid"


def fee(p: float | np.ndarray) -> float | np.ndarray:
    return FEE_K * p * (1.0 - p)


# --------------------------------------------------------------------------- LUT bucket grid (mirrors src/hightempbot/calibration/lut.py)

# 8 buckets covering [0, 1] — same boundaries as the live bot
LUT_BUCKETS = [
    (0.00, 0.02),
    (0.02, 0.05),
    (0.05, 0.10),
    (0.10, 0.15),
    (0.15, 0.25),
    (0.25, 0.40),
    (0.40, 0.60),
    (0.60, 1.00),
]


def bucket_low_for(p: float) -> float:
    """Map probability to its LUT bucket's lower bound."""
    if p < 0:
        return LUT_BUCKETS[0][0]
    for lo, hi in LUT_BUCKETS:
        if lo <= p < hi:
            return lo
    return LUT_BUCKETS[-1][0]  # 1.0 lands in the final right-inclusive bucket


# --------------------------------------------------------------------------- ensemble + EMOS

# Sigma floor matches src/hightempbot/calibration/emos.py::_SIGMA_FLOOR.
# Was 0.5°C in this file from creation until 2026-05-09 — diverged from live
# (which used 0.1°C from its creation), producing a backtest Gaussian that was
# 5× wider-floored than what the live bot trades against. Aligned to live
# 2026-05-09 after the parity audit. See wiki/meta/parity-report-src-vs-backtest.
#
# Diagnostic finding 2026-05-09: sigma floor accounts for ~$3 of the $121 BR100
# train PnL shift; the bracket-parser P0-2 fix accounts for the other ~$118.
# Bracket parsing was the dominant driver, not sigma floor.
_SIGMA_FLOOR = 0.1  # °C


def predict_emos(a: float, b: float, c: float, d: float, ensemble: np.ndarray) -> tuple[float, float]:
    em = ensemble.mean()
    ev = ensemble.var(ddof=0) if len(ensemble) > 1 else 0.0
    mu = a + b * em
    sigma2 = math.exp(c) + math.exp(d) * ev
    sigma = math.sqrt(max(sigma2, _SIGMA_FLOOR ** 2))
    return mu, sigma


def emos_p_in_bracket(mu: float, sigma: float, lo: float, hi: float) -> float:
    pl = 0.0 if lo == -math.inf else norm.cdf(lo, mu, sigma)
    ph = 1.0 if hi == math.inf else norm.cdf(hi, mu, sigma)
    return float(max(0.0, min(1.0, ph - pl)))


# --------------------------------------------------------------------------- walk-forward loaders

# Exponential recency half-lives (days) for the p_Recency_h{hl} LUT flavor (plan
# 2026-05-29-002 U3). The recency-weighted hit rate down-weights stale bucket
# observations so seasonal drift in calibration fades from the estimate.
LUT_RECENCY_HALF_LIVES = (15, 30)


def load_walk_forward_emos(live_conn) -> pd.DataFrame:
    """All historical EMOS params, sorted for merge_asof.

    Returns DataFrame with columns: station_id, asof_date (datetime64), a, b, c, d.
    """
    rows = []
    for sid, asof, blob in live_conn.execute(
        "SELECT station_id, asof_date, params_blob FROM calibration_params_history WHERE horizon=1"
    ):
        try:
            d = json.loads(blob)
            rows.append((sid, asof, d["a"], d["b"], d["c"], d["d"]))
        except Exception:
            continue
    df = pd.DataFrame(rows, columns=["station_id", "asof_date", "a", "b", "c", "d"])
    df["asof_date"] = pd.to_datetime(df["asof_date"])
    return df.sort_values(["station_id", "asof_date"]).reset_index(drop=True)


def load_walk_forward_lut(live_conn, half_lives: tuple[int, ...] = LUT_RECENCY_HALF_LIVES) -> pd.DataFrame:
    """Cumulative (n, hits) and recency-decayed sums per (station, pred_bucket_low)
    up through each local_date.

    Returns DataFrame with: station_id, pred_bucket_low, local_date (datetime64),
    n_cum, hits_cum, and for each half-life hl the columns w{hl} (sum of decayed
    weights = effective sample size) and wh{hl} (decayed hit sum).

    The recency-weighted hit rate as of any market_date is wh{hl}/w{hl} taken from
    the latest row with local_date < market_date. Because both numerator and
    denominator scale by the same extra decay over the (market_date - last_obs)
    gap, the ratio is gap-invariant — so the merge_asof match carries the correct
    decayed estimate without needing the market_date at accumulation time.

    Walk-forward semantics: when joining, use local_date < market_date (strict).
    """
    df = pd.read_sql(
        "SELECT station_id, local_date, pred_bucket_low, hit "
        "FROM pred_bucket_history WHERE local_date IS NOT NULL",
        live_conn,
    )
    df["local_date"] = pd.to_datetime(df["local_date"])
    df = df.sort_values(["station_id", "pred_bucket_low", "local_date"]).reset_index(drop=True)
    df["n_cum"] = df.groupby(["station_id", "pred_bucket_low"]).cumcount() + 1
    df["hits_cum"] = df.groupby(["station_id", "pred_bucket_low"])["hit"].cumsum()

    # Recency-decayed accumulators: one sequential pass per (station, bucket) group
    # (rows are already sorted by group then local_date, so groups are contiguous).
    hit = df["hit"].fillna(0.0).to_numpy(dtype=float)
    day = df["local_date"].to_numpy("datetime64[D]")
    group_id = df.groupby(["station_id", "pred_bucket_low"]).ngroup().to_numpy()
    n = len(df)
    for hl in half_lives:
        w = np.empty(n, dtype=float)
        wh = np.empty(n, dtype=float)
        cw = 0.0
        cwh = 0.0
        prev_g = -1
        prev_day = None
        for i in range(n):
            g = group_id[i]
            if g != prev_g:
                cw = 0.0
                cwh = 0.0
                prev_day = None
                prev_g = g
            if prev_day is not None:
                gap_days = (day[i] - prev_day) / np.timedelta64(1, "D")
                decay = 0.5 ** (gap_days / float(hl))
                cw *= decay
                cwh *= decay
            cw += 1.0
            cwh += hit[i]
            w[i] = cw
            wh[i] = cwh
            prev_day = day[i]
        df[f"w{hl}"] = w
        df[f"wh{hl}"] = wh

    keep = ["station_id", "pred_bucket_low", "local_date", "n_cum", "hits_cum"]
    keep += [f"w{hl}" for hl in half_lives] + [f"wh{hl}" for hl in half_lives]
    return df[keep].reset_index(drop=True)


def lut_lookup_for_rows(rows: pd.DataFrame, lut_cum: pd.DataFrame) -> pd.DataFrame:
    """For each row in `rows` (must have station_id, market_date, p_raw_for_bucket),
    return a copy with n_cum, hits_cum filled via merge_asof on local_date < market_date.

    rows.market_date and rows.p_raw_for_bucket must be present.
    Adds columns: pred_bucket_low, n_cum, hits_cum.
    """
    rows = rows.copy()
    rows["pred_bucket_low"] = rows["p_raw_for_bucket"].apply(bucket_low_for)
    rows["market_date_dt"] = pd.to_datetime(rows["market_date"])
    rows = rows.sort_values("market_date_dt").reset_index(drop=True)

    lut_cum = lut_cum.sort_values("local_date").reset_index(drop=True)

    # merge_asof requires both sides sorted by the asof key.
    # We need: latest lut row where local_date < market_date_dt (strict less-than).
    # Use direction='backward' with allow_exact_matches=False to enforce strict inequality.
    out = pd.merge_asof(
        rows,
        lut_cum,
        left_on="market_date_dt",
        right_on="local_date",
        by=["station_id", "pred_bucket_low"],
        direction="backward",
        allow_exact_matches=False,
    )
    return out


def emos_for_rows(rows: pd.DataFrame, emos_hist: pd.DataFrame) -> pd.DataFrame:
    """For each row (with station_id, market_date), attach as-of EMOS (a,b,c,d).

    Uses asof_date <= market_date. Direction='backward', allow_exact_matches=True.
    """
    rows = rows.copy()
    rows["market_date_dt"] = pd.to_datetime(rows["market_date"])
    rows = rows.sort_values("market_date_dt").reset_index(drop=True)
    emos_hist = emos_hist.sort_values("asof_date").reset_index(drop=True)

    out = pd.merge_asof(
        rows,
        emos_hist,
        left_on="market_date_dt",
        right_on="asof_date",
        by="station_id",
        direction="backward",
        allow_exact_matches=True,
    )
    return out


# --------------------------------------------------------------------------- ensembles + actuals

def load_ensembles(live_conn, start_date: str, end_date: str) -> dict[tuple[str, str], np.ndarray]:
    """Load forecast ensembles in the exact live model order.

    Historical DB snapshots may contain extra centers from older ingestion
    experiments (for example ``jma_seamless``). The live bot scores only the
    calibrated 9-model set, so the backtest does the same: keep a group only
    when every expected model is present, ignore extras, and return arrays in
    ``EXPECTED_MODELS`` order.
    """
    grouped: dict[tuple[str, str], dict[str, float]] = {}
    for sid, td, centre, t in live_conn.execute(
        "SELECT station_id, target_date, centre, tmax_celsius FROM forecast_archive "
        "WHERE horizon=1 AND target_date BETWEEN ? AND ? AND tmax_celsius IS NOT NULL",
        (start_date, end_date),
    ):
        if centre not in EXPECTED_MODELS:
            continue
        grouped.setdefault((sid, td), {})[str(centre)] = float(t)

    expected = set(EXPECTED_MODELS)
    out: dict[tuple[str, str], np.ndarray] = {}
    for key, by_model in grouped.items():
        if set(by_model) != expected:
            continue
        out[key] = np.array([by_model[m] for m in EXPECTED_MODELS], dtype=float)
    return out


def load_actuals(live_conn) -> dict[tuple[str, str], float]:
    out = {}
    for sid, d, t in live_conn.execute(
        "SELECT station_id, local_date, tmax_celsius FROM actuals "
        "WHERE source='wu' AND tmax_celsius IS NOT NULL"
    ):
        out[(sid, d)] = float(t)
    return out


# --------------------------------------------------------------------------- entry-time prices

def load_station_timezones(live_conn) -> dict[str, str]:
    """{icao: 'America/New_York', ...} for all enrolled stations."""
    return {sid: tz for sid, tz in live_conn.execute(
        "SELECT icao, timezone FROM enrolled_stations WHERE timezone IS NOT NULL"
    )}


def load_entry_prices_by_local_hour(
    market_conn,
    station_tz: dict[str, str],
    candidate_hours: tuple[int, ...] = ENTRY_LOCAL_HOURS,
) -> dict[str, dict]:
    """For each market_slug, return {yes_price_h{H}, no_price_h{H}, entry_ts_h{H}}
    for each H in candidate_hours.

    "Local hour H" means: latest snapshot whose timestamp converted to the
    station's local TZ is ≤ market_date H:00:00 local. So h=12 = the price
    you'd see at noon local on market_date.

    Snapshots without enough history before H simply have NaN for that column.
    """
    # Per-market: list of (ts_unix, yes_price, no_price) sorted ascending.
    # We aggregate yes/no into one row per ts so we can do one pass per market.
    by_slug: dict[str, dict[int, dict]] = {}

    for slug, market_date, station, side, ts, price in market_conn.execute("""
        SELECT m.market_slug, m.market_date, m.station_id, p.side, p.ts_unix, p.price
        FROM markets m JOIN prices p USING (market_slug)
        ORDER BY m.market_slug, p.ts_unix
    """):
        d = by_slug.setdefault(slug, {})
        ent = d.setdefault(ts, {"market_date": market_date, "station_id": station,
                                "yes": None, "no": None})
        if side == "Yes":
            ent["yes"] = price
        elif side == "No":
            ent["no"] = price

    metrics_by_slug: dict[str, list[tuple[int, float, float, float]]] = {}
    for slug, ts, vol, liq, spr in market_conn.execute(
        "SELECT market_slug, ts_unix, volume, liquidity, spread FROM metrics ORDER BY market_slug, ts_unix"
    ):
        metrics_by_slug.setdefault(slug, []).append(
            (
                ts,
                vol if vol is not None else 0.0,
                liq if liq is not None else 0.0,
                spr if spr is not None else 0.0,
            )
        )

    def metrics_at_or_before(slug: str, entry_ts: int | None) -> tuple[float | None, float | None, float | None, int | None]:
        if entry_ts is None:
            return None, None, None, None
        chosen = None
        for ts, vol, liq, spr in metrics_by_slug.get(slug, []):
            if ts <= entry_ts:
                chosen = (vol, liq, spr, ts)
            else:
                break
        if chosen is None:
            return None, None, None, None
        return chosen

    # Now per market, walk timestamps in order and pick latest-≤-cutoff per H.
    out: dict[str, dict] = {}
    tz_cache: dict[str, pytz.tzinfo.BaseTzInfo] = {}

    for slug, snaps in by_slug.items():
        # Need a station_id and market_date — use any snap (all share the same).
        first = next(iter(snaps.values()))
        station = first["station_id"]
        market_date = first["market_date"]

        tz_name = station_tz.get(station)
        if not tz_name:
            continue
        tz = tz_cache.setdefault(tz_name, pytz.timezone(tz_name))

        # Cutoff timestamps for each candidate H = market_date H:00:00 local → unix
        try:
            md_dt = datetime.strptime(market_date, "%Y-%m-%d")
        except ValueError:
            continue
        cutoffs = {}
        for H in candidate_hours:
            local_dt = tz.localize(md_dt.replace(hour=H, minute=0, second=0))
            cutoffs[H] = int(local_dt.timestamp())

        # Sort snapshots by ts ascending, scan once, remember last yes/no seen
        # for each H cutoff.
        sorted_ts = sorted(snaps.keys())
        result: dict = {}
        last_yes = last_no = None
        last_yes_ts = last_no_ts = None
        # For each H in ascending cutoff order, pick the latest snapshot with ts <= cutoff
        cutoff_pairs = sorted(cutoffs.items(), key=lambda x: x[1])
        i = 0  # snapshot pointer
        for H, cutoff in cutoff_pairs:
            while i < len(sorted_ts) and sorted_ts[i] <= cutoff:
                ts = sorted_ts[i]
                snap = snaps[ts]
                if snap["yes"] is not None:
                    last_yes = snap["yes"]; last_yes_ts = ts
                if snap["no"] is not None:
                    last_no = snap["no"]; last_no_ts = ts
                i += 1
            result[f"yes_price_h{H}"] = last_yes
            result[f"no_price_h{H}"] = last_no
            entry_ts = max(last_yes_ts or 0, last_no_ts or 0) or None
            result[f"entry_ts_h{H}"] = entry_ts
            vol, liq, spr, metrics_ts = metrics_at_or_before(slug, entry_ts)
            result[f"entry_volume_h{H}"] = vol
            result[f"entry_liquidity_h{H}"] = liq
            result[f"entry_spread_h{H}"] = spr
            result[f"entry_metrics_ts_h{H}"] = metrics_ts
        out[slug] = result
    return out


def load_entry_prices(market_conn, hours_before_close: int = 4) -> dict[str, dict]:
    """For each market_slug, latest YES/NO price >= hours_before_close before market_date+1 00:00 UTC.

    Approximation: bracket close treated as market_date 23:59 UTC. For US stations that's
    actually some hours short of local midnight close, which is fine — we still avoid
    using info from the final hours.

    Returns {market_slug: {yes_price, yes_ts, no_price, no_ts, avg_volume, avg_liquidity, avg_spread}}.
    """
    cur = market_conn.execute("""
        SELECT m.market_slug, m.market_date, p.side, p.ts_unix, p.price
        FROM markets m JOIN prices p USING (market_slug)
        ORDER BY m.market_slug, p.ts_unix
    """)
    out: dict[str, dict] = {}
    cutoff_by_date: dict[str, int] = {}
    for slug, mdate, side, ts, price in cur:
        cutoff = cutoff_by_date.get(mdate)
        if cutoff is None:
            close_utc = int(datetime.fromisoformat(mdate + "T00:00:00+00:00").timestamp()) + 86400
            cutoff = close_utc - hours_before_close * 3600
            cutoff_by_date[mdate] = cutoff
        if ts > cutoff:
            continue
        side_key = side.lower()
        if side_key not in ("yes", "no"):
            continue
        d = out.setdefault(slug, {"yes_price": None, "yes_ts": None, "no_price": None, "no_ts": None})
        d[f"{side_key}_price"] = price
        d[f"{side_key}_ts"] = ts

    # Lifetime-averaged metrics (kept for reference / backward compatibility)
    for slug, vol_avg, liq_avg, spr_avg in market_conn.execute(
        "SELECT market_slug, AVG(volume), AVG(liquidity), AVG(spread) FROM metrics GROUP BY market_slug"
    ):
        if slug in out:
            out[slug]["avg_volume"] = vol_avg
            out[slug]["avg_liquidity"] = liq_avg
            out[slug]["avg_spread"] = spr_avg

    # Per-snapshot-at-entry metrics: liquidity/volume/spread at the moment we'd bet.
    # Polymarket metrics are recorded ~every 10 min. Pick the snapshot whose ts_unix
    # is the latest <= entry_ts_unix (so we use only data available at decision time).
    metrics_by_slug: dict[str, list[tuple[int, float, float, float]]] = {}
    for slug, ts, vol, liq, spr in market_conn.execute(
        "SELECT market_slug, ts_unix, volume, liquidity, spread FROM metrics ORDER BY market_slug, ts_unix"
    ):
        metrics_by_slug.setdefault(slug, []).append(
            (ts, vol if vol is not None else 0.0,
             liq if liq is not None else 0.0,
             spr if spr is not None else 0.0)
        )
    for slug, d in out.items():
        entry_ts = d.get("yes_ts") or d.get("no_ts")
        if entry_ts is None:
            d["entry_volume"] = None; d["entry_liquidity"] = None; d["entry_spread"] = None
            continue
        snaps = metrics_by_slug.get(slug, [])
        # Find latest snap with ts <= entry_ts
        chosen = None
        for ts, vol, liq, spr in snaps:
            if ts <= entry_ts:
                chosen = (ts, vol, liq, spr)
            else:
                break
        if chosen is None:
            d["entry_volume"] = None; d["entry_liquidity"] = None; d["entry_spread"] = None
        else:
            d["entry_volume"] = chosen[1]
            d["entry_liquidity"] = chosen[2]
            d["entry_spread"] = chosen[3]
            d["entry_metrics_ts"] = chosen[0]
    return out


# --------------------------------------------------------------------------- signal flavors

def add_signal_flavors(df: pd.DataFrame, lut_min_n: int = 30) -> pd.DataFrame:
    """Attach 9 p_model_<flavor> columns. Inputs needed: p_raw, n_cum, hits_cum."""
    df = df.copy()
    E = df["p_raw"].astype(float)
    n = df["n_cum"].fillna(0).astype(float)
    hits = df["hits_cum"].fillna(0).astype(float)

    L_obs = np.where(n > 0, hits / np.maximum(n, 1), np.nan)
    confident = n >= lut_min_n

    df["p_E"]          = E
    df["p_L_strict"]   = np.where(confident, L_obs, np.nan)
    df["p_L_loose"]    = np.where(confident, L_obs, E)
    df["p_B_50"]       = 0.5 * E + 0.5 * df["p_L_loose"]
    df["p_B_30"]       = 0.3 * E + 0.7 * df["p_L_loose"]
    df["p_B_70"]       = 0.7 * E + 0.3 * df["p_L_loose"]
    df["p_Shrink_n10"] = (hits + 10.0 * E) / (n + 10.0)
    df["p_Shrink_n50"] = (hits + 50.0 * E) / (n + 50.0)

    lam = np.minimum(n / 50.0, 1.0)
    L_safe = np.where(np.isnan(L_obs), E, L_obs)
    df["p_Ramp"] = lam * L_safe + (1.0 - lam) * E

    # Recency-weighted LUT flavor (plan 2026-05-29-002 U3). Requires the decayed
    # weight columns from load_walk_forward_lut; absent (e.g. the live parity
    # test feeds only p_raw/n_cum/hits_cum) it is silently skipped. Shrinks toward
    # the EMOS prior E with a pseudo-count of 10, mirroring p_Shrink_n10's form so
    # sparse buckets fall back to E rather than to a noisy recent rate.
    for hl in LUT_RECENCY_HALF_LIVES:
        wcol, whcol = f"w{hl}", f"wh{hl}"
        if wcol in df.columns and whcol in df.columns:
            w = df[wcol].fillna(0.0).astype(float)
            wh = df[whcol].fillna(0.0).astype(float)
            df[f"p_Recency_h{hl}"] = (wh + 10.0 * E) / (w + 10.0)

    return df


SIGNAL_COLUMNS = [
    "p_E", "p_L_strict", "p_L_loose",
    "p_B_50", "p_B_30", "p_B_70",
    "p_Shrink_n10", "p_Shrink_n50", "p_Ramp",
] + [f"p_Recency_h{hl}" for hl in LUT_RECENCY_HALF_LIVES]


# --------------------------------------------------------------------------- leakage asserts

@dataclass
class LeakageReport:
    n_rows: int
    emos_violations: int
    lut_violations: int
    entry_violations: int


def assert_no_leakage(df: pd.DataFrame, hours_before_close: int = 4) -> LeakageReport:
    """Hard asserts for walk-forward correctness. Raises on any violation.

    Required columns: market_date, asof_date (EMOS), local_date (LUT, may be NaT for cold start),
    entry_ts_unix, close_ts_unix.
    """
    md = pd.to_datetime(df["market_date"])
    emos_asof = pd.to_datetime(df["asof_date"])
    lut_local = pd.to_datetime(df["local_date"])

    emos_bad = (emos_asof > md).fillna(False)
    # NaT for lut means "no LUT data yet" (cold start), not a violation
    lut_bad = ((lut_local >= md) & lut_local.notna()).fillna(False)
    entry_bad = ((df["entry_ts_unix"] + hours_before_close * 3600) > df["close_ts_unix"]).fillna(False)

    n_emos = int(emos_bad.sum())
    n_lut = int(lut_bad.sum())
    n_entry = int(entry_bad.sum())

    if n_emos or n_lut or n_entry:
        sample = df[emos_bad | lut_bad | entry_bad].head(3).to_dict("records")
        raise AssertionError(
            f"LEAKAGE: emos_violations={n_emos} lut_violations={n_lut} entry_violations={n_entry}\n"
            f"first offending rows: {sample}"
        )
    return LeakageReport(n_rows=len(df), emos_violations=0, lut_violations=0, entry_violations=0)


# --------------------------------------------------------------------------- evaluate config (the function ce:optimize calls)

def evaluate_config(df: pd.DataFrame, config: dict, train_end: str = "2026-04-04",
                    test_end: str = "2026-05-02") -> dict:
    """Evaluate one strategy config against the decision table.

    Config keys:
        side: 'YES' or 'NO'
        signal: one of SIGNAL_COLUMNS (e.g. 'p_E', 'p_L_loose', ...)
        min_edge: float
        max_edge: float
        min_fill_price: float (YES = max acceptable yes_price; NO = min acceptable no_price)
        max_fill_price: float (optional, default 1.0 for YES, 1.0 for NO upper bound)
        bracket_kind: 'low'|'mid'|'high'|'all' (optional, default 'all')
        min_liquidity: float (optional, default 0)
        min_lut_n: int (optional, default 0; filters rows where n_cum < min_lut_n)
        min_alpha_ratio: float (optional, default 0; require p >= alpha * fill_price.
            For YES: p >= alpha * yes_price. For NO: (1-p) >= alpha * no_price.
            Use this for cheap-tail betting where additive edge is uninformative.)

    Returns dict of metrics: train_pnl, test_pnl, train_n, test_n, train_sharpe, test_sharpe,
    train_win_rate, test_win_rate, train_avg_edge.
    """
    side = config["side"]
    sig = config["signal"]
    min_edge = float(config["min_edge"])
    max_edge = float(config["max_edge"])
    min_fp = float(config["min_fill_price"])
    max_fp = float(config.get("max_fill_price", 1.0))
    bk = config.get("bracket_kind", "all")
    min_liq = float(config.get("min_liquidity", 0.0))
    min_lut_n = int(config.get("min_lut_n", 0))
    min_alpha = float(config.get("min_alpha_ratio", 0.0))
    entry_h = config.get("entry_local_hour")  # int in ENTRY_LOCAL_HOURS, or None for default

    if sig not in df.columns:
        raise ValueError(f"unknown signal {sig!r}; choose from {SIGNAL_COLUMNS}")

    if entry_h is None:
        yp_col = "yes_price"
        np_col = "no_price"
        liq_col = "entry_liquidity"
        ts_col = "entry_ts_unix"
    else:
        if int(entry_h) not in ENTRY_LOCAL_HOURS:
            raise ValueError(f"entry_local_hour={entry_h} not in {ENTRY_LOCAL_HOURS}")
        yp_col = f"yes_price_h{int(entry_h)}"
        np_col = f"no_price_h{int(entry_h)}"
        liq_col = f"entry_liquidity_h{int(entry_h)}"
        ts_col = f"entry_ts_h{int(entry_h)}"
        missing = [c for c in (yp_col, np_col, liq_col, ts_col) if c not in df.columns]
        if missing:
            raise ValueError(
                f"columns {missing!r} missing; rebuild the base backtest decision table"
            )

    p = df[sig].to_numpy()
    yp = df[yp_col].to_numpy(dtype=float)
    np_p = df[np_col].to_numpy(dtype=float)

    if side == "YES":
        edge = p - yp - fee(yp)
        fp_ok = (yp >= min_fp) & (yp <= max_fp)
        pnl = np.where(df["won_yes"].to_numpy() == 1, 1.0 - yp, -yp)
        alpha_ok = (p >= min_alpha * yp) if min_alpha > 0 else np.ones_like(p, dtype=bool)
    else:
        edge = (1.0 - p) - np_p - fee(np_p)
        fp_ok = (np_p >= min_fp) & (np_p <= max_fp)
        pnl = np.where(df["won_yes"].to_numpy() == 0, 1.0 - np_p, -np_p)
        alpha_ok = ((1.0 - p) >= min_alpha * np_p) if min_alpha > 0 else np.ones_like(p, dtype=bool)

    # Filter out rows with no entry price for the chosen hour
    price_ok = ~np.isnan(yp) & ~np.isnan(np_p)
    entry_ts = df[ts_col].to_numpy(dtype=float)
    if entry_h is None:
        not_leakage = ~np.isnan(entry_ts)
    else:
        # Force seconds explicitly — `astype("int64") // 10**9` assumes the
        # underlying datetime64 unit is nanoseconds (true on pandas <=2.x but
        # NOT on pandas 3.x where the default is microseconds), which silently
        # collapsed this leakage gate. See live_match_eval.py:349 for the same
        # fix.
        md_unix = pd.to_datetime(df["market_date"]).astype("datetime64[s]").astype("int64").to_numpy()
        not_leakage = ~np.isnan(entry_ts) & (entry_ts >= md_unix)
    sel = (edge >= min_edge) & (edge <= max_edge) & fp_ok & alpha_ok & ~np.isnan(p) & price_ok & not_leakage
    if bk != "all":
        sel &= (df["bracket_kind"].to_numpy() == bk)
    if min_liq > 0:
        sel &= (df[liq_col].fillna(0).to_numpy() >= min_liq)
    if min_lut_n > 0:
        sel &= (df["n_cum"].fillna(0).to_numpy() >= min_lut_n)

    md = df["market_date"].to_numpy()
    is_train = (md >= "2026-02-04") & (md <= train_end)
    is_test = (md > train_end) & (md <= test_end)

    # Honest-reporting inputs (Phase 2): claimed P(win), realized win, per-bet
    # stake. For a YES bet claimed=p, stake=yes_price; for a NO bet claimed=1-p,
    # stake=no_price. `won` is whether the bet's side resolved in the money.
    won_yes = df["won_yes"].to_numpy()
    if side == "YES":
        claimed = p.astype(float)
        won = (won_yes == 1)
        staked = yp
    else:
        claimed = (1.0 - p).astype(float)
        won = (won_yes == 0)
        staked = np_p

    return {
        **_metrics(pnl, edge, sel & is_train, prefix="train_",
                   claimed=claimed, won=won, staked=staked),
        **_metrics(pnl, edge, sel & is_test, prefix="test_",
                   claimed=claimed, won=won, staked=staked),
        "config": config,
    }


def _metrics(pnl: np.ndarray, edge: np.ndarray, sel: np.ndarray, prefix: str,
             claimed: np.ndarray | None = None, won: np.ndarray | None = None,
             staked: np.ndarray | None = None) -> dict:
    """Summarize a selected slice.

    Existing keys ({prefix}n/pnl/sharpe/win_rate/avg_edge) are preserved for
    backward compatibility. When claimed/won/staked are supplied, honest keys
    are APPENDED: win-rate SE, mean claimed P(win) vs realized frequency,
    overconfidence (pp), per-stake ROS, and a compact reliability table. This is
    what makes model overconfidence and the razor-thin ROS margin visible in
    every summary, not just PnL.
    """
    n = int(sel.sum())
    if n == 0:
        base = {f"{prefix}n": 0, f"{prefix}pnl": 0.0, f"{prefix}sharpe": 0.0,
                f"{prefix}win_rate": 0.0, f"{prefix}avg_edge": 0.0}
        if claimed is not None:
            base.update({
                f"{prefix}win_rate_se": 0.0, f"{prefix}claimed_mean": 0.0,
                f"{prefix}realized_mean": 0.0, f"{prefix}overconf_pp": 0.0,
                f"{prefix}ros": 0.0, f"{prefix}staked": 0.0,
                f"{prefix}reliability": [],
            })
        return base
    p = pnl[sel]
    out = {
        f"{prefix}n": n,
        f"{prefix}pnl": float(p.sum()),
        f"{prefix}sharpe": float(p.mean() / (p.std(ddof=0) + 1e-9)),
        f"{prefix}win_rate": float((p > 0).mean()),
        f"{prefix}avg_edge": float(edge[sel].mean()),
    }
    if claimed is not None and won is not None and staked is not None:
        recs = [
            {"claimed_p": float(c), "won": bool(w), "stake": float(s),
             "pnl": float(pp), "entry_ts": 0}
            for c, w, s, pp in zip(claimed[sel], won[sel], staked[sel], p)
        ]
        wr_wins = int(np.asarray(won)[sel].sum())
        st = float(np.asarray(staked)[sel].sum())
        cm = float(np.asarray(claimed)[sel].mean())
        rm = wr_wins / n
        out.update({
            f"{prefix}win_rate_se": round(hr.proportion_se(wr_wins, n), 4),
            f"{prefix}claimed_mean": round(cm, 4),
            f"{prefix}realized_mean": round(rm, 4),
            f"{prefix}overconf_pp": round((cm - rm) * 100.0, 2),
            f"{prefix}ros": round(float(p.sum()) / st, 4) if st > 0 else 0.0,
            f"{prefix}staked": round(st, 4),
            f"{prefix}reliability": hr.reliability_table(recs),
        })
    return out
