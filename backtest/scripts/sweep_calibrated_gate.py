"""Walk-forward sweep for the market-aware calibrated NO gate.

Answers one question: *what edge gate should replace the current 9pp NO gate?*

Protocol (the project's expanding ABCD convention, ``date_chunks(md, 4)``):

    A                fit-only  (no earlier chunk to fit a curve from)
    B  fit on A      -> eval B
    C  fit on A+B    -> eval C
    D  fit on A+B+C  -> eval D

For every evaluation window the market-aware logit blend
``calibrated = sigmoid(b0 + b1*logit(claimed) + b2*logit(no_price))`` is refit
**per group** (NO_F / NO_C) on *only the data strictly before the window*, then
scored on the bets it fires *inside that window* — so the fired slice is always
out of the fit's selection. A degeneracy guard (``blend_degenerate_reason``:
b1>0, b2>=0, price_weight<=0.90) rejects a window's fit; on rejection we fall
back to the last healthy curve for that group and note it. The 1D isotonic
baseline is refit the same expanding way.

The gate SWEEP varies only the edge band; the fill logic / pre-filters are held
identical to ``validate_calibrated_gate.py`` so only the gate differs:

    * entry/fill price   = ``no_price``
    * pre-filters        = n_cum >= 30, avg_volume >= 50, price > 0
    * strict path        = price in [0.75, 1.0], edge in [min_edge, strict_cap]
    * ceiling extension  = bracket_kind == 'high', signal 1-p_B_50, price in
                           [0.50, 1.0], edge in [min_edge, ceil_cap]; only when
                           strict missed (mirrors the live gate exactly).
    * edge               = calibrated - price - fee,  fee = 0.05*p*(1-p)
    * min_edge  in {0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05}  (blend)
    * cap       in {on (strict 0.15 / ceil 0.35), off (inf/inf)}
    * units     in {F-only, C-only, both}
    * stake     = $10 flat

Baselines scored under the SAME walk-forward protocol:
    * raw 9pp gate (transform=identity, edge in [0.090, 0.15]) — F-only & both
    * 1D isotonic gate (refit per window)     — F-only & both

Honest metrics per variant come from ``backtest/lib/honest_report.py``
(read-only): n (+bets/day), win rate +/-SE, claimed-vs-realized (rel-gap),
ROS, PnL@$10, maxDD proxy, per-window B/C/D breakdown, reliability table.

Usage:
    python backtest/scripts/sweep_calibrated_gate.py \
        --parquet backtest/data/decision_table_may11plus_l2.parquet
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))            # backtest.lib.*
sys.path.insert(0, str(_REPO_ROOT / "src"))    # hightempbot.*

from hightempbot.calibration.reliability import (  # noqa: E402
    ReliabilityCurve,
    blend_degenerate_reason,
)
from backtest.lib.honest_report import (  # noqa: E402
    proportion_se,
    reliability_table,
    slice_metrics,
)

# --- constants held identical to validate_calibrated_gate.py -----------------
CLAIMED_LO = 0.70
CLAIMED_HI = 1.0
FEE_THETA = 0.05
NO_MIN_N = 30
NO_MIN_VOL = 50.0
STRICT_FP_MIN = 0.75
CEIL_FP_MIN = 0.50
NO_FP_MAX = 1.00
STAKE = 10.0

# baseline (current live) band
RAW_MIN_EDGE = 0.090
RAW_STRICT_CAP = 0.15
RAW_CEIL_CAP = 0.35

# blend sweep
MIN_EDGES = (0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05)
UNITS = ("F", "C", "both")

# logit/sigmoid clamp (matches reliability._LOGIT_EPS)
_EPS = 1e-6


# --------------------------------------------------------------------------- fee / pnl
def _fee_arr(price: np.ndarray) -> np.ndarray:
    return FEE_THETA * price * (1.0 - price)


def _pnl_arr(won_no: np.ndarray, price: np.ndarray) -> np.ndarray:
    """Net-of-entry-fee PnL of a $10 NO bet, vectorized. Mirrors validate."""
    shares = STAKE / price
    entry_fee = shares * _fee_arr(price)
    win = shares * 1.0 - STAKE - entry_fee
    loss = -STAKE - entry_fee
    return np.where(won_no >= 0.5, win, loss)


# --------------------------------------------------------------------------- transforms
def _logit(p: np.ndarray) -> np.ndarray:
    q = np.clip(p, _EPS, 1.0 - _EPS)
    return np.log(q / (1.0 - q))


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _apply_iso_vec(curve: ReliabilityCurve, x: np.ndarray) -> np.ndarray:
    """Vectorized clamped linear interp over PAV breakpoints (== module apply)."""
    if not curve.breakpoints:
        return np.clip(x, 0.0, 1.0)
    bp = np.asarray(curve.breakpoints, dtype=float)
    vals = np.asarray(curve.values, dtype=float)
    out = np.interp(x, bp, vals, left=vals[0], right=vals[-1])
    return np.clip(out, 0.0, 1.0)


def _apply_blend_vec(coefs: list[float], claimed: np.ndarray, price: np.ndarray) -> np.ndarray:
    """sigmoid(b0 + b1*logit(claimed) + b2*logit(price)); identity if <3 coefs."""
    if len(coefs) < 3:
        return np.clip(claimed, 0.0, 1.0)
    b0, b1, b2 = coefs[0], coefs[1], coefs[2]
    return np.clip(_sigmoid(b0 + b1 * _logit(claimed) + b2 * _logit(price)), 0.0, 1.0)


# --------------------------------------------------------------------------- data prep
@dataclass
class WindowData:
    label: str
    start: str
    end: str
    idx: np.ndarray          # row indices of this window in the full frame


@dataclass
class FitInfo:
    """Curves fit on data strictly before a window, plus fallback notes."""
    blend: dict[str, list[float]]              # group -> coefs (possibly fallback)
    iso: dict[str, ReliabilityCurve]
    notes: list[str]


def date_chunks(market_dates, n_chunks: int = 4) -> list[tuple[str, str, str]]:
    """Replicate lut_range_chunks.date_chunks: chronological np.array_split."""
    dates = sorted({str(d) for d in market_dates})
    split = np.array_split(np.array(dates, dtype=object), n_chunks)
    out = []
    for off, part in enumerate(split):
        if len(part) == 0:
            continue
        out.append((chr(ord("A") + off), str(part[0]), str(part[-1])))
    return out


def _group_arr(labels) -> np.ndarray:
    """'NO_F' / 'NO_C' / '' per bracket label (degree token, like the live gate)."""
    out = np.empty(len(labels), dtype=object)
    for i, l in enumerate(labels):
        s = str(l)
        out[i] = "NO_F" if "°F" in s else ("NO_C" if "°C" in s else "")
    return out


def _fit_group_blend(df: pd.DataFrame, prior_mask: np.ndarray, group: str,
                     raw_claimed: np.ndarray, price: np.ndarray, won_no: np.ndarray,
                     grp: np.ndarray) -> list[float]:
    """Fit the logit blend for one group on prior candidates (claimed in
    [0.70,1] & price in (0,1)) — the exact fit population validate uses."""
    m = (
        prior_mask
        & (grp == group)
        & (raw_claimed >= CLAIMED_LO) & (raw_claimed <= CLAIMED_HI)
        & (price > 0.0) & (price < 1.0)
        & np.isfinite(raw_claimed) & np.isfinite(price)
    )
    triples = list(zip(raw_claimed[m].tolist(), price[m].tolist(), won_no[m].tolist()))
    curve = ReliabilityCurve.fit_blend(triples, group_key=group)
    return curve.coefs


def _fit_group_iso(prior_mask: np.ndarray, group: str, raw_claimed: np.ndarray,
                   won_no: np.ndarray, grp: np.ndarray) -> ReliabilityCurve:
    m = (
        prior_mask
        & (grp == group)
        & (raw_claimed >= CLAIMED_LO) & (raw_claimed <= CLAIMED_HI)
        & np.isfinite(raw_claimed)
    )
    pairs = list(zip(raw_claimed[m].tolist(), won_no[m].tolist()))
    return ReliabilityCurve.fit(pairs, group_key=group)


# --------------------------------------------------------------------------- gate replay
def fire_mask(*, transform: str, min_edge: float, strict_cap: float, ceil_cap: float,
              units: str, base_ok: np.ndarray, price: np.ndarray, fee: np.ndarray,
              grp: np.ndarray, bracket_high: np.ndarray, pb50_ok: np.ndarray,
              claimed_strict: np.ndarray, claimed_ceil: np.ndarray):
    """Return (fired, claimed_used) boolean/float arrays for one gate variant.

    claimed_* are already the transformed (calibrated) claims for the strict
    (1-p_E) and ceiling (1-p_B_50) signals under ``transform``.
    """
    unit_ok = np.ones_like(base_ok)
    if units == "F":
        unit_ok = grp == "NO_F"
    elif units == "C":
        unit_ok = grp == "NO_C"
    ok = base_ok & unit_ok

    edge_s = claimed_strict - price - fee
    strict_fire = ok & (price >= STRICT_FP_MIN) & (price <= NO_FP_MAX) \
        & (edge_s >= min_edge) & (edge_s <= strict_cap)

    edge_c = claimed_ceil - price - fee
    ceil_fire = ok & bracket_high & pb50_ok & (~strict_fire) \
        & (price >= CEIL_FP_MIN) & (price <= NO_FP_MAX) \
        & (edge_c >= min_edge) & (edge_c <= ceil_cap)

    fired = strict_fire | ceil_fire
    claimed_used = np.where(strict_fire, claimed_strict,
                            np.where(ceil_fire, claimed_ceil, np.nan))
    return fired, claimed_used


def make_records(fired: np.ndarray, claimed_used: np.ndarray, won_no: np.ndarray,
                 price: np.ndarray, pnl: np.ndarray, entry_ts: np.ndarray,
                 grp: np.ndarray, period: str) -> list[dict]:
    recs = []
    for i in np.where(fired)[0]:
        recs.append({
            "period": period,
            "strategy": "NO",
            "bracket_unit": "F" if grp[i] == "NO_F" else "C",
            "claimed_p": float(claimed_used[i]),
            "entry_price": float(price[i]),
            "stake": STAKE,
            "won": bool(won_no[i] >= 0.5),
            "pnl": float(pnl[i]),
            "entry_ts": int(entry_ts[i]),
        })
    return recs


# --------------------------------------------------------------------------- driver
def run(df: pd.DataFrame) -> dict:
    labels = df["bracket_label"].to_numpy()
    grp = _group_arr(labels)
    md = df["market_date"].astype(str).to_numpy()
    raw_claimed = 1.0 - df["p_E"].to_numpy(dtype=float)
    price = df["no_price"].to_numpy(dtype=float)
    won_no = 1.0 - df["won_yes"].to_numpy(dtype=float)
    n_cum = df["n_cum"].to_numpy(dtype=float)
    vol = df["avg_volume"].to_numpy(dtype=float)
    bracket_high = (df["bracket_kind"].astype(str).to_numpy() == "high")
    pb50 = df["p_B_50"].to_numpy(dtype=float)
    raw_ceil = 1.0 - pb50
    pb50_ok = np.isfinite(pb50)
    entry_ts = df["entry_ts_unix"].to_numpy(dtype=np.int64)
    fee = _fee_arr(price)

    # Shared pre-filters (identical to validate). NaN vol -> excluded.
    base_ok = (
        (grp != "")
        & np.isfinite(price) & (price > 0.0)
        & np.isfinite(n_cum) & (n_cum >= NO_MIN_N)
        & np.isfinite(vol) & (vol >= NO_MIN_VOL)
        & np.isfinite(raw_claimed)
    )

    chunks = date_chunks(md, 4)
    windows = []
    for lab, s, e in chunks:
        idx = np.where((md >= s) & (md <= e))[0]
        windows.append(WindowData(lab, s, e, idx))
    eval_windows = [w for w in windows if w.label != "A"]  # B,C,D

    # ---- expanding walk-forward fits (strictly-before-window) ----
    fits: dict[str, FitInfo] = {}
    last_healthy: dict[str, list[float]] = {"NO_F": [], "NO_C": []}
    for w in eval_windows:
        prior_mask = md < w.start
        notes = []
        blend = {}
        iso = {}
        for g in ("NO_F", "NO_C"):
            coefs = _fit_group_blend(df, prior_mask, g, raw_claimed, price, won_no, grp)
            reason = blend_degenerate_reason(coefs)
            if reason is None:
                blend[g] = coefs
                last_healthy[g] = coefs
            else:
                fb = last_healthy[g]
                blend[g] = fb
                notes.append(
                    f"{w.label}/{g}: blend degenerate ({reason}); "
                    + ("fell back to prior-window curve" if fb else "no prior healthy curve -> IDENTITY")
                )
            iso[g] = _fit_group_iso(prior_mask, g, raw_claimed, won_no, grp)
        fits[w.label] = FitInfo(blend=blend, iso=iso, notes=notes)

    # ---- precompute transformed claims per window (strict + ceiling signals) ----
    # transformed[window][transform] = (claimed_strict_arr, claimed_ceil_arr) restricted to window idx
    def transformed_for(w: WindowData, transform: str):
        idx = w.idx
        rc = raw_claimed[idx]
        rc_ceil = raw_ceil[idx]
        pr = price[idx]
        f = fits[w.label]
        if transform == "raw":
            return rc.copy(), rc_ceil.copy()
        out_s = np.empty_like(rc)
        out_c = np.empty_like(rc_ceil)
        g_idx = grp[idx]
        for g in ("NO_F", "NO_C"):
            gm = g_idx == g
            if transform == "1d":
                out_s[gm] = _apply_iso_vec(f.iso[g], rc[gm])
                out_c[gm] = _apply_iso_vec(f.iso[g], rc_ceil[gm])
            else:  # blend
                out_s[gm] = _apply_blend_vec(f.blend[g], rc[gm], pr[gm])
                out_c[gm] = _apply_blend_vec(f.blend[g], rc_ceil[gm], pr[gm])
        other = ~((g_idx == "NO_F") | (g_idx == "NO_C"))
        out_s[other] = rc[other]
        out_c[other] = rc_ceil[other]
        return out_s, out_c

    def eval_variant(transform, min_edge, cap_on, units):
        """Return per-window records + OOS-concat records for one gate variant."""
        strict_cap = RAW_STRICT_CAP if cap_on else np.inf
        ceil_cap = RAW_CEIL_CAP if cap_on else np.inf
        per_window = {}
        oos = []
        for w in eval_windows:
            idx = w.idx
            cs, cc = transformed_for(w, transform)
            fired, claimed_used = fire_mask(
                transform=transform, min_edge=min_edge, strict_cap=strict_cap,
                ceil_cap=ceil_cap, units=units, base_ok=base_ok[idx],
                price=price[idx], fee=fee[idx], grp=grp[idx],
                bracket_high=bracket_high[idx], pb50_ok=pb50_ok[idx],
                claimed_strict=cs, claimed_ceil=cc,
            )
            recs = make_records(
                fired, claimed_used, won_no[idx], price[idx],
                _pnl_arr(won_no[idx], price[idx]), entry_ts[idx], grp[idx], w.label,
            )
            per_window[w.label] = recs
            oos.extend(recs)
        return per_window, oos

    return {
        "windows": windows,
        "eval_windows": eval_windows,
        "fits": fits,
        "eval_variant": eval_variant,
        "n_oos_days": sum(len({d for d in md[w.idx]}) for w in eval_windows),
    }


# --------------------------------------------------------------------------- reporting
def _rgap_pp(m: dict) -> float:
    return m["overconfidence_pp"]  # (claimed_mean - realized)*100


def variant_row(name, transform, min_edge, cap_on, units, per_window, oos, n_oos_days):
    m = slice_metrics(oos)
    win_days = {"B": 23, "C": 23, "D": 22}
    pw = {}
    for w in ("B", "C", "D"):
        mm = slice_metrics(per_window.get(w, []))
        pw[w] = mm
    return {
        "name": name,
        "transform": transform,
        "min_edge": min_edge,
        "cap": "0.15" if cap_on else "none",
        "units": units,
        "n": m["n"],
        "bets_per_day": round(m["n"] / n_oos_days, 2) if n_oos_days else 0.0,
        "win_rate": m["win_rate"],
        "win_se": m["win_rate_se"],
        "cal_clm": m["claimed_mean"],
        "realized": m["realized_mean"],
        "rel_gap_pp": _rgap_pp(m),
        "ros_pct": m["ros_pct"],
        "pnl_10": m["pnl"],
        "maxdd_abs": m["max_dd_abs"],
        "B_n": pw["B"]["n"], "B_pnl": pw["B"]["pnl"], "B_ros": pw["B"]["ros_pct"],
        "C_n": pw["C"]["n"], "C_pnl": pw["C"]["pnl"], "C_ros": pw["C"]["ros_pct"],
        "D_n": pw["D"]["n"], "D_pnl": pw["D"]["pnl"], "D_ros": pw["D"]["ros_pct"],
        "_oos_records": oos,
        "_pw": pw,
    }


def passes_recommendation(r: dict, min_bets_per_day: float = 3.0) -> tuple[bool, list[str]]:
    fails = []
    # rel-gap within +/-3pp where n>=50
    if r["n"] >= 50 and abs(r["rel_gap_pp"]) > 3.0:
        fails.append(f"rel_gap {r['rel_gap_pp']:+.1f}pp exceeds +/-3pp (n={r['n']})")
    # positive ROS every window
    for w in ("B", "C", "D"):
        if r[f"{w}_n"] == 0:
            fails.append(f"{w}: 0 bets")
        elif r[f"{w}_ros"] <= 0:
            fails.append(f"{w} ROS {r[f'{w}_ros']:+.2f}% not positive")
    # >= 3 bets/day
    if r["bets_per_day"] < min_bets_per_day:
        fails.append(f"{r['bets_per_day']:.2f} bets/day < {min_bets_per_day}")
    return (len(fails) == 0), fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--parquet",
                    default=str(_REPO_ROOT / "backtest/data/decision_table_may11plus_l2.parquet"))
    ap.add_argument("--csv", default=str(_REPO_ROOT / "backtest/results/sweep_calibrated_gate.csv"))
    args = ap.parse_args(argv)

    df = pd.read_parquet(args.parquet)
    ctx = run(df)
    ev = ctx["eval_variant"]
    n_oos_days = ctx["n_oos_days"]

    # ----- assemble variants -----
    rows = []

    # baselines (same walk-forward protocol)
    for units in ("F", "both"):
        pw, oos = ev("raw", RAW_MIN_EDGE, True, units)
        rows.append(variant_row(f"BASE raw9pp/{units}", "raw", RAW_MIN_EDGE, True, units, pw, oos, n_oos_days))
    for units in ("F", "both"):
        pw, oos = ev("1d", RAW_MIN_EDGE, True, units)
        rows.append(variant_row(f"BASE 1d9pp/{units}", "1d", RAW_MIN_EDGE, True, units, pw, oos, n_oos_days))

    # blend sweep
    for units in UNITS:
        for cap_on in (True, False):
            for me in MIN_EDGES:
                pw, oos = ev("blend", me, cap_on, units)
                cap = "cap" if cap_on else "unc"
                nm = f"blend {me:.3f}/{units}/{cap}"
                rows.append(variant_row(nm, "blend", me, cap_on, units, pw, oos, n_oos_days))

    # ----- print fit / fallback notes -----
    print(f"OOS days (B+C+D): {n_oos_days}")
    print("\nWalk-forward blend fits (fit strictly before each window):")
    for w in ctx["eval_windows"]:
        f = ctx["fits"][w.label]
        print(f"  window {w.label} [{w.start}..{w.end}]  fit on md < {w.start}")
        for g in ("NO_F", "NO_C"):
            c = f.blend[g]
            if len(c) >= 3:
                pwgt = abs(c[2]) / (abs(c[1]) + abs(c[2]))
                print(f"    {g}: b0={c[0]:+.3f} b1={c[1]:+.3f} b2={c[2]:+.3f} price_weight={pwgt:.3f}")
            else:
                print(f"    {g}: IDENTITY (no coefs)")
        for note in f.notes:
            print(f"    NOTE {note}")
    # Highlight the load-bearing OOS structural fact.
    f_healthy = any(len(ctx["fits"][w.label].blend["NO_F"]) >= 3 for w in ctx["eval_windows"])
    c_healthy = any(len(ctx["fits"][w.label].blend["NO_C"]) >= 3 for w in ctx["eval_windows"])
    print(f"\n  STRUCTURAL: NO_F blend healthy in >=1 window? {f_healthy}  |  "
          f"NO_C blend healthy in >=1 window? {c_healthy}")
    if not f_healthy:
        print("  => The degeneracy guard REFUSES the °F blend in every window, so blend/F falls\n"
              "     back to identity: 'blend/F' is byte-identical to a raw-claim gate. OOS the\n"
              "     market-aware calibration exists ONLY for °C (windows C, D).")

    # ----- full sweep table -----
    hdr = (f"{'variant':<24}{'n':>5}{'/day':>6}{'win%':>7}{'SE':>5}{'cal':>7}{'real':>7}"
           f"{'gap_pp':>8}{'ROS%':>8}{'PnL@10':>9}{'maxDD':>8}"
           f"{'B_pnl':>8}{'C_pnl':>8}{'D_pnl':>8}{'+win':>5}")
    print("\n" + "=" * len(hdr))
    print("FULL SWEEP TABLE (walk-forward B+C+D, $10 flat)")
    print("=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        pos = sum(1 for w in ("B", "C", "D") if r[f"{w}_ros"] > 0)
        print(f"{r['name']:<24}{r['n']:>5}{r['bets_per_day']:>6.2f}"
              f"{r['win_rate']*100:>7.1f}{r['win_se']*100:>5.1f}"
              f"{r['cal_clm']*100:>7.1f}{r['realized']*100:>7.1f}"
              f"{r['rel_gap_pp']:>+8.1f}{r['ros_pct']:>+8.2f}{r['pnl_10']:>+9.2f}"
              f"{r['maxdd_abs']:>8.2f}{r['B_pnl']:>+8.1f}{r['C_pnl']:>+8.1f}{r['D_pnl']:>+8.1f}"
              f"{pos:>4}/3")

    # ----- recommendation -----
    blend_rows = [r for r in rows if r["transform"] == "blend"]
    scored = []
    for r in blend_rows:
        ok, fails = passes_recommendation(r)
        scored.append((r, ok, fails))
    passing = [r for r, ok, _ in scored if ok]
    passing.sort(key=lambda r: r["pnl_10"], reverse=True)

    print("\n" + "=" * 80)
    print("RECOMMENDATION")
    print("=" * 80)
    print("Criteria: max OOS PnL@$10 s.t. |rel_gap|<=3pp (n>=50), ROS>0 in B,C,D, >=3 bets/day.")
    if passing:
        best = passing[0]
        print(f"\n  WINNER: {best['name']}")
        _print_variant(best)
        # sensitivity: neighbors (same units/cap, adjacent min_edge)
        print("\n  Sensitivity (same units/cap, neighboring thresholds):")
        fam = [r for r in blend_rows if r["units"] == best["units"] and r["cap"] == best["cap"]]
        fam.sort(key=lambda r: r["min_edge"])
        for r in fam:
            mark = " <== winner" if r["name"] == best["name"] else ""
            ok, _ = passes_recommendation(r)
            print(f"    me={r['min_edge']:.3f} n={r['n']:>4} /day={r['bets_per_day']:>5.2f} "
                  f"ROS={r['ros_pct']:>+6.2f}% PnL@10={r['pnl_10']:>+8.2f} gap={r['rel_gap_pp']:>+5.1f}pp "
                  f"pass={'Y' if ok else 'N'}{mark}")
    else:
        print("\n  NO blend variant satisfies all criteria. Closest by OOS PnL among those")
        print("  meeting rel-gap + all-window-positive (relaxing the >=3 bets/day bar):")
        relaxed = []
        for r, ok, fails in scored:
            # relax only the bets/day bar
            r_fails = [f for f in fails if "bets/day" not in f]
            if not r_fails:
                relaxed.append(r)
        relaxed.sort(key=lambda r: r["pnl_10"], reverse=True)
        for r in relaxed[:5]:
            print(f"    {r['name']:<22} n={r['n']:>4} /day={r['bets_per_day']:>5.2f} "
                  f"ROS={r['ros_pct']:>+6.2f}% PnL@10={r['pnl_10']:>+8.2f} gap={r['rel_gap_pp']:>+5.1f}pp")
        if relaxed:
            print("\n  Best-by-PnL detail (bets/day bar relaxed):")
            _print_variant(relaxed[0])

    # risk-adjusted knee: max-PnL degenerates to max-volume, so also surface the
    # threshold that best trades total PnL against per-bet ROS + drawdown.
    print("\n  RISK-ADJUSTED KNEE (max PnL degenerates to max volume; ROS/DD matter under")
    print("  the bot's capital caps, which the flat-$10 objective ignores):")
    for fam_units, fam_cap in (("both", "0.15"), ("both", "none")):
        fam = sorted([r for r in blend_rows if r["units"] == fam_units and r["cap"] == fam_cap],
                     key=lambda r: r["min_edge"])
        print(f"    frontier units={fam_units} cap={fam_cap}:")
        for r in fam:
            ok, _ = passes_recommendation(r)
            print(f"      me={r['min_edge']:.3f} n={r['n']:>4} /day={r['bets_per_day']:>5.2f} "
                  f"ROS={r['ros_pct']:>+6.2f}% PnL@10={r['pnl_10']:>+8.2f} "
                  f"maxDD=${r['maxdd_abs']:>6.2f} gap={r['rel_gap_pp']:>+5.1f}pp pass={'Y' if ok else 'N'}")

    # baseline comparison
    print("\n  Baseline (current live raw 9pp gate) under same walk-forward:")
    for r in rows:
        if r["transform"] == "raw":
            print(f"    {r['name']:<22} n={r['n']:>4} /day={r['bets_per_day']:>5.2f} "
                  f"ROS={r['ros_pct']:>+6.2f}% PnL@10={r['pnl_10']:>+8.2f} gap={r['rel_gap_pp']:>+5.1f}pp "
                  f"(B/C/D pnl {r['B_pnl']:+.1f}/{r['C_pnl']:+.1f}/{r['D_pnl']:+.1f})")

    # ----- write CSV -----
    out = Path(args.csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["name", "transform", "min_edge", "cap", "units", "n", "bets_per_day",
            "win_rate", "win_se", "cal_clm", "realized", "rel_gap_pp", "ros_pct",
            "pnl_10", "maxdd_abs", "B_n", "B_pnl", "B_ros", "C_n", "C_pnl", "C_ros",
            "D_n", "D_pnl", "D_ros"]
    with out.open("w", newline="", encoding="utf-8") as fh:
        wtr = csv.DictWriter(fh, fieldnames=cols)
        wtr.writeheader()
        for r in rows:
            wtr.writerow({c: r[c] for c in cols})
    print(f"\nWrote full sweep table: {out}")
    return 0


def _print_variant(r: dict) -> None:
    print(f"    config: transform=blend  min_edge={r['min_edge']:.3f}  "
          f"cap={r['cap']}  units={r['units']}")
    print(f"    OOS: n={r['n']} ({r['bets_per_day']:.2f}/day)  "
          f"win={r['win_rate']*100:.1f}%+/-{r['win_se']*100:.1f}  "
          f"cal_clm={r['cal_clm']*100:.1f}%  realized={r['realized']*100:.1f}%  "
          f"rel_gap={r['rel_gap_pp']:+.1f}pp")
    print(f"    OOS: ROS={r['ros_pct']:+.2f}%  PnL@$10=${r['pnl_10']:+.2f}  "
          f"maxDD(proxy)=${r['maxdd_abs']:.2f}")
    for w in ("B", "C", "D"):
        m = r["_pw"][w]
        print(f"      {w}: n={m['n']:>4}  win={m['win_rate']*100:>5.1f}%  "
              f"ROS={m['ros_pct']:>+6.2f}%  PnL=${m['pnl']:>+7.2f}  "
              f"rel_gap={m['overconfidence_pp']:>+5.1f}pp")
    rel = reliability_table(r["_oos_records"])
    print("    Reliability (OOS fired slice): claimed bin -> realized")
    for row in rel:
        print(f"      [{row['bin_lo']:.2f},{row['bin_hi']:.2f}] n={row['n']:>4} "
              f"claimed={row['claimed_mean']*100:>5.1f}% realized={row['realized_freq']*100:>5.1f}% "
              f"gap={row['gap_pp']:>+5.1f}pp")


if __name__ == "__main__":
    sys.exit(main())
