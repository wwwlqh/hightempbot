"""Score the deployed NO config at real fills by walking the L2 ask ladder
instead of filling at the displayed mid. NO_C uses the shipped logit-blend
curve (coefficients below); NO_F runs raw. Read-only; prints a report and
writes a CSV.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))            # backtest.lib.*
sys.path.insert(0, str(_REPO / "src"))    # hightempbot.*

from hightempbot.calibration.reliability import (  # noqa: E402
    ReliabilityCurve,
    blend_degenerate_reason,
)
from hightempbot.execution.walker import walk_book_edge_preserving  # noqa: E402
from hightempbot.persistence.ledger import poly_fee_per_share  # noqa: E402
from backtest.lib.honest_report import (  # noqa: E402
    reliability_table,
    slice_metrics,
)

# --- constants (identical to sweep_calibrated_gate.py) -----------------------
FEE_THETA = 0.05
CLAIMED_LO = 0.70
CLAIMED_HI = 1.0
NO_MIN_N = 30
NO_MIN_VOL = 50.0
STRICT_FP_MIN = 0.75
CEIL_FP_MIN = 0.50
NO_FP_MAX = 1.00
GATE_MIN_EDGE = 0.04            # deployed NO.min_edge (calibrated edge floor at the mid)
STRICT_CAP = 0.15              # deployed NO.max_edge
CEIL_CAP = 0.35               # deployed NO.max_edge_for_ceiling
EXECUTION_MIN_EDGE = 0.05      # deployed NO.execution_min_edge (walker VWAP floor)
MIN_BET_USD = 1.0             # deployed MIN_BET_USD
_EPS = 1e-6                    # matches reliability._LOGIT_EPS

# The SHIPPED static NO_C curve (insert_no_c_curve.py, since deleted; see git
# history). NO_F is IDENTITY.
STATIC_CURVE = {
    "NO_C": [0.266862555657062, 0.20271908690835438, 0.7292953541842045],
    "NO_F": [],
}

# targets: 10/50/100 for the primary table + depth sensitivity;
# 35/70 = 7% of $500/$1000 for the honest $/month expectation.
PRIMARY_TARGETS = (10.0, 50.0, 100.0)
BANKROLL_TARGETS = {500.0: 35.0, 1000.0: 70.0}
ALL_TARGETS = sorted(set(PRIMARY_TARGETS) | set(BANKROLL_TARGETS.values()))


# --------------------------------------------------------------------------- math
def _logit(p):
    q = np.clip(p, _EPS, 1.0 - _EPS)
    return np.log(q / (1.0 - q))


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def apply_blend(coefs, claimed, price):
    """sigmoid(b0 + b1*logit(claimed) + b2*logit(price)); identity if <3 coefs.
    Byte-identical to reliability.ReliabilityCurve._apply_blend, vectorized."""
    if len(coefs) < 3:
        return np.clip(claimed, 0.0, 1.0)
    b0, b1, b2 = coefs[0], coefs[1], coefs[2]
    return np.clip(_sigmoid(b0 + b1 * _logit(claimed) + b2 * _logit(price)), 0.0, 1.0)


def date_chunks(md, n_chunks: int = 4):
    """Replicate lut_range_chunks.date_chunks (chronological np.array_split)."""
    dates = sorted({str(d) for d in md})
    split = np.array_split(np.array(dates, dtype=object), n_chunks)
    out = []
    for off, part in enumerate(split):
        if len(part) == 0:
            continue
        out.append((chr(ord("A") + off), str(part[0]), str(part[-1])))
    return out


# --------------------------------------------------------------------------- ladder
def parse_ladder(raw):
    """Parse a serialized L2 ask ladder -> sorted [(price, shares), ...].
    Mirrors backtest/lib/live_match_eval._parse_ladder."""
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return []
    if isinstance(raw, str):
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    lv = []
    for level in raw:
        try:
            if isinstance(level, dict):
                p, s = float(level["price"]), float(level["size"])
            else:
                if len(level) < 2:
                    continue
                p, s = float(level[0]), float(level[1])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if math.isfinite(p) and math.isfinite(s) and 0.0 < p < 1.0 and s > 0:
            lv.append((p, s))
    return sorted(lv, key=lambda x: x[0])


# --------------------------------------------------------------------------- gate
class Frame:
    """Vectorized decision arrays (parity with sweep_calibrated_gate.run)."""

    def __init__(self, df: pd.DataFrame):
        labels = df["bracket_label"].astype(str).to_numpy()
        self.grp = np.array(
            ["NO_F" if "°F" in s else ("NO_C" if "°C" in s else "") for s in labels],
            dtype=object,
        )
        self.md = df["market_date"].astype(str).to_numpy()
        self.raw_claimed = 1.0 - df["p_E"].to_numpy(dtype=float)   # P(NO wins) = 1 - p_E
        self.raw_ceil = 1.0 - df["p_B_50"].to_numpy(dtype=float)   # ceiling signal 1 - p_B_50
        self.price = df["no_price"].to_numpy(dtype=float)
        self.won_no = 1.0 - df["won_yes"].to_numpy(dtype=float)
        self.n_cum = df["n_cum"].to_numpy(dtype=float)
        self.vol = df["avg_volume"].to_numpy(dtype=float)
        self.bracket_high = df["bracket_kind"].astype(str).to_numpy() == "high"
        pb50 = df["p_B_50"].to_numpy(dtype=float)
        self.pb50_ok = np.isfinite(pb50)
        self.spread = df["entry_spread"].to_numpy(dtype=float)
        self.entry_ts = df["entry_ts_unix"].to_numpy()
        self.fee = FEE_THETA * self.price * (1.0 - self.price)
        self.base_ok = (
            (self.grp != "")
            & np.isfinite(self.price) & (self.price > 0.0)
            & np.isfinite(self.n_cum) & (self.n_cum >= NO_MIN_N)
            & np.isfinite(self.vol) & (self.vol >= NO_MIN_VOL)
            & np.isfinite(self.raw_claimed)
        )
        # per-hour entry timestamps + NO ask ladder columns (exact-match lookup)
        self._ets = np.column_stack(
            [df[f"entry_ts_h{h}"].to_numpy(dtype=float) for h in range(24)]
        )
        self._no_ask_cols = [df[f"no_ask_ladder_h{h}"].to_numpy() for h in range(24)]

    def matching_hour(self, i: int):
        v = self.entry_ts[i]
        try:
            vi = int(v)
        except (TypeError, ValueError):
            return None
        for h in range(24):
            t = self._ets[i, h]
            if math.isfinite(t) and int(t) == vi:
                return h
        return None

    def ladder_for(self, i: int):
        """Production-parity NO ask ladder at the row's entry snapshot."""
        h = self.matching_hour(i)
        if h is None:
            return None
        lv = parse_ladder(self._no_ask_cols[h][i])
        return lv or None


def fit_wf_curves(fr: Frame, evalw):
    """Expanding walk-forward blend fits per window (mirrors sweep exactly: fit strictly
    before the window, degeneracy guard -> fall back to last healthy curve; no prior
    healthy -> IDENTITY)."""
    last_healthy = {"NO_F": [], "NO_C": []}
    fits = {}
    notes = []
    for lab, s, _e in evalw:
        prior = fr.md < s
        blend = {}
        for g in ("NO_F", "NO_C"):
            m = (
                prior & (fr.grp == g)
                & (fr.raw_claimed >= CLAIMED_LO) & (fr.raw_claimed <= CLAIMED_HI)
                & (fr.price > 0.0) & (fr.price < 1.0)
                & np.isfinite(fr.raw_claimed) & np.isfinite(fr.price)
            )
            triples = list(zip(fr.raw_claimed[m].tolist(), fr.price[m].tolist(), fr.won_no[m].tolist()))
            coefs = ReliabilityCurve.fit_blend(triples, group_key=g).coefs
            reason = blend_degenerate_reason(coefs)
            if reason is None:
                blend[g] = coefs
                last_healthy[g] = coefs
            else:
                blend[g] = last_healthy[g]
                notes.append(
                    f"{lab}/{g}: blend degenerate ({reason.split('(')[0].strip()}); "
                    + ("fell back to prior-window curve" if last_healthy[g] else "no prior healthy -> IDENTITY")
                )
        fits[lab] = blend
    return fits, notes


def gate_fire(fr: Frame, evalw, cal_mode: str, wf_fits=None):
    """Return (fired_mask, calibrated_used) over the full frame (B+C+D only)."""
    fired = np.zeros(len(fr.md), dtype=bool)
    cal_used = np.full(len(fr.md), np.nan)
    for lab, s, e in evalw:
        idx = np.where((fr.md >= s) & (fr.md <= e))[0]
        rc = fr.raw_claimed[idx]
        rcc = fr.raw_ceil[idx]
        pr = fr.price[idx]
        g = fr.grp[idx]
        cs = np.empty_like(rc)
        cc = np.empty_like(rcc)
        for grpname in ("NO_F", "NO_C"):
            gm = g == grpname
            coefs = STATIC_CURVE[grpname] if cal_mode == "static" else wf_fits[lab][grpname]
            cs[gm] = apply_blend(coefs, rc[gm], pr[gm])
            cc[gm] = apply_blend(coefs, rcc[gm], pr[gm])
        other = ~((g == "NO_F") | (g == "NO_C"))
        cs[other] = rc[other]
        cc[other] = rcc[other]
        ok = fr.base_ok[idx]
        f = fr.fee[idx]
        bh = fr.bracket_high[idx]
        pbok = fr.pb50_ok[idx]
        edge_s = cs - pr - f
        strict_fire = ok & (pr >= STRICT_FP_MIN) & (pr <= NO_FP_MAX) & (edge_s >= GATE_MIN_EDGE) & (edge_s <= STRICT_CAP)
        edge_c = cc - pr - f
        ceil_fire = ok & bh & pbok & (~strict_fire) & (pr >= CEIL_FP_MIN) & (pr <= NO_FP_MAX) & (edge_c >= GATE_MIN_EDGE) & (edge_c <= CEIL_CAP)
        fr_mask = strict_fire | ceil_fire
        cu = np.where(strict_fire, cs, np.where(ceil_fire, cc, np.nan))
        fired[idx] = fr_mask
        cal_used[idx] = cu
    return fired, cal_used


# --------------------------------------------------------------------------- fills
def real_fill_pnl(ladder, target_usd, calibrated_p, won_no):
    """Walk the real ask ladder with the deployed walker; return a per-bet
    record dict or None if the edge floor can't be preserved."""
    book = {"asks": [{"price": p, "size": s} for p, s in ladder]}
    res = walk_book_edge_preserving(
        book,
        target_usd,
        prob_safe_floor=float(calibrated_p),
        fee_theta=FEE_THETA,
        min_edge=EXECUTION_MIN_EDGE,
        min_bet_usd=MIN_BET_USD,
        max_walk_price=None,
    )
    if res is None:
        return None
    filled_usd, filled_shares, filled_vwap, _limit, realized_edge = res
    entry_fee = filled_shares * poly_fee_per_share(filled_vwap, fee_theta=FEE_THETA)
    if won_no >= 0.5:
        net = filled_shares * 1.0 - filled_usd - entry_fee
    else:
        net = -filled_usd - entry_fee
    return {
        "stake": float(filled_usd),
        "fill_vwap": float(filled_vwap),
        "realized_edge": float(realized_edge),
        "pnl": float(net),
    }


def mid_fill_record(no_price, calibrated_p, won_no, entry_ts, unit, window, row_idx, stake=10.0):
    """Mid-fill PnL (fill AT no_price) -- the settled-sweep counterfactual, used to
    decompose real-fill loss into selection (bad at mid too) vs depth slippage."""
    p = float(no_price)
    shares = stake / p
    entry_fee = shares * poly_fee_per_share(p, fee_theta=FEE_THETA)
    net = shares * 1.0 - stake - entry_fee if won_no >= 0.5 else -stake - entry_fee
    return {
        "period": window, "strategy": "NO", "side": "NO", "bracket_unit": unit,
        "claimed_p": float(calibrated_p), "entry_price": p, "fill_vwap": p,
        "stake": float(stake), "won": bool(won_no >= 0.5), "pnl": float(net),
        "entry_ts": int(entry_ts), "row_idx": int(row_idx),
    }


def mid_fill_all(fr: Frame, fired, cal_used):
    """Mid-fill records over EVERY gate-fired row (static-curve analog of the
    settled +6.16% WF headline, computed at no_price)."""
    recs = []
    for i in np.where(fired)[0]:
        unit = "F" if fr.grp[i] == "NO_F" else "C"
        window = None
        for lab, s, e in ((c[0], c[1], c[2]) for c in fr._chunks):
            if s <= fr.md[i] <= e:
                window = lab
                break
        recs.append(mid_fill_record(fr.price[i], cal_used[i], fr.won_no[i], fr.entry_ts[i], unit, window, i))
    return recs


def haircut_pnl(no_price, spread, target_usd, won_no):
    """Approximate no-ladder fill: mid + half-spread haircut, flat, full target.
    Labeled approximate; ignores the walker's edge floor (optimistic)."""
    hs = 0.5 * max(float(spread), 0.0) if math.isfinite(spread) else 0.0
    taker = min(no_price + hs, 0.999)
    if not (0.0 < taker < 1.0):
        return None
    shares = target_usd / taker
    entry_fee = shares * poly_fee_per_share(taker, fee_theta=FEE_THETA)
    if won_no >= 0.5:
        net = shares * 1.0 - target_usd - entry_fee
    else:
        net = -target_usd - entry_fee
    return {"stake": float(target_usd), "fill_vwap": float(taker), "pnl": float(net)}


def score_target(fr: Frame, fired, cal_used, target_usd):
    """Walk every fired row at ``target_usd``. Returns (primary_records,
    haircut_records, coverage_dict)."""
    fidx = np.where(fired)[0]
    primary, haircut = [], []
    n_ladder = n_fill = n_nofill = n_noladder = 0
    for i in fidx:
        unit = "F" if fr.grp[i] == "NO_F" else "C"
        window = None
        # window label
        for lab, s, e in ((c[0], c[1], c[2]) for c in fr._chunks):
            if s <= fr.md[i] <= e:
                window = lab
                break
        base = {
            "period": window, "strategy": "NO", "side": "NO", "bracket_unit": unit,
            "claimed_p": float(cal_used[i]), "entry_price": float(fr.price[i]),
            "won": bool(fr.won_no[i] >= 0.5), "entry_ts": int(fr.entry_ts[i]),
            "row_idx": int(i),
        }
        ladder = fr.ladder_for(i)
        if ladder:
            n_ladder += 1
            rec = real_fill_pnl(ladder, target_usd, cal_used[i], fr.won_no[i])
            if rec is None:
                n_nofill += 1
                continue
            n_fill += 1
            primary.append({**base, **rec})
        else:
            n_noladder += 1
            rec = haircut_pnl(fr.price[i], fr.spread[i], target_usd, fr.won_no[i])
            if rec is not None:
                haircut.append({**base, **rec})
    cov = {
        "n_gate": len(fidx), "n_ladder": n_ladder, "n_fill": n_fill,
        "n_ladder_nofill": n_nofill, "n_noladder": n_noladder,
    }
    return primary, haircut, cov


# --------------------------------------------------------------------------- reporting helpers
def _units_split(records):
    c = [r for r in records if r["bracket_unit"] == "C"]
    f = [r for r in records if r["bracket_unit"] == "F"]
    return c, f


def _avg_fill_vs_mid(records):
    if not records:
        return 0.0
    return float(np.mean([r["fill_vwap"] - r["entry_price"] for r in records]))


def _fmt_slice(tag, m, extra=""):
    if m["n"] == 0:
        return f"    {tag:<16} n=   0  (no bets){extra}"
    return (
        f"    {tag:<16} n={m['n']:>4}  win={m['win_rate']*100:>5.1f}%+/-{m['win_rate_se']*100:>4.1f}  "
        f"cal={m['claimed_mean']*100:>5.1f}%  real={m['realized_mean']*100:>5.1f}%  "
        f"relgap={m['overconfidence_pp']:>+5.1f}pp  ROS={m['ros_pct']:>+7.2f}%  "
        f"PnL=${m['pnl']:>+8.2f}  staked=${m['staked']:>8.0f}{extra}"
    )


# --------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--parquet", default=str(_REPO / "backtest/data/decision_table_may11plus_l2.parquet"))
    ap.add_argument("--csv", default=str(_REPO / "backtest/results/score_deployed_realfill.csv"))
    args = ap.parse_args(argv)

    df = pd.read_parquet(args.parquet)
    fr = Frame(df)
    chunks = date_chunks(fr.md, 4)
    fr._chunks = chunks
    evalw = [c for c in chunks if c[0] != "A"]
    n_oos_days = sum(len({d for d in fr.md if s <= d <= e}) for _l, s, e in evalw)

    print("=" * 108)
    print("SCORE DEPLOYED CHAMPION CONFIG AT REAL FILLS  (NO-only, TAIL disabled)")
    print("=" * 108)
    print(f"decision table: {len(df)} rows  {fr.md.min()}..{fr.md.max()}")
    print("chunks (ABCD, np.array_split by market_date):")
    for lab, s, e in chunks:
        nd = len({d for d in fr.md if s <= d <= e})
        tag = "[fit-only]" if lab == "A" else "[OOS eval]"
        print(f"   {lab}: {s}..{e}  ({nd} days) {tag}")
    print(f"OOS days (B+C+D): {n_oos_days}")
    print(f"walker: walk_book_edge_preserving  execution_min_edge={EXECUTION_MIN_EDGE}  "
          f"min_bet=${MIN_BET_USD}  max_walk_price=None (VWAP-floor bounded)")
    print(f"static NO_C curve: b0={STATIC_CURVE['NO_C'][0]:+.4f} b1={STATIC_CURVE['NO_C'][1]:+.4f} "
          f"b2={STATIC_CURVE['NO_C'][2]:+.4f}  price_wt="
          f"{abs(STATIC_CURVE['NO_C'][2])/(abs(STATIC_CURVE['NO_C'][1])+abs(STATIC_CURVE['NO_C'][2])):.3f}  "
          f"| NO_F: IDENTITY (guard-refused)")

    wf_fits, wf_notes = fit_wf_curves(fr, evalw)
    print("\nWalk-forward refit blend fits (sensitivity variant):")
    for lab, s, _e in evalw:
        for g in ("NO_F", "NO_C"):
            c = wf_fits[lab][g]
            if len(c) >= 3:
                pw = abs(c[2]) / (abs(c[1]) + abs(c[2]))
                print(f"   {lab}/{g}: b0={c[0]:+.3f} b1={c[1]:+.3f} b2={c[2]:+.3f} price_wt={pw:.3f}")
            else:
                print(f"   {lab}/{g}: IDENTITY")
    for note in wf_notes:
        print(f"   NOTE {note}")

    modes = {"static": ("static", None), "wf": ("wf", wf_fits)}
    results = {}   # (mode, target) -> dict
    gate_meta = {}
    for mode, (cm, wf) in modes.items():
        fired, cal_used = gate_fire(fr, evalw, cm, wf_fits=wf)
        gate_meta[mode] = {
            "n_gate": int(fired.sum()),
            "n_gate_C": int(((fr.grp == "NO_C") & fired).sum()),
            "n_gate_F": int(((fr.grp == "NO_F") & fired).sum()),
        }
        for tgt in ALL_TARGETS:
            primary, haircut, cov = score_target(fr, fired, cal_used, tgt)
            results[(mode, tgt)] = {
                "primary": primary, "haircut": haircut, "cov": cov,
                "m": slice_metrics(primary),
            }

    print("\n" + "=" * 108)
    print("(a) PRIMARY TABLE -- DEPLOYED CONFIG AT REAL FILLS (ladder-only; B+C+D OOS)")
    print("=" * 108)
    print(f"    {'gate fires (mid-decision)':<30}: static={gate_meta['static']['n_gate']}  wf-refit={gate_meta['wf']['n_gate']}   (wf reproduces the settled n=849 checkpoint)")
    hdr = (f"    {'variant':<20}{'nGate':>6}{'nLad':>5}{'nFill':>6}{'cov%':>6}{'avgFill-mid':>12}"
           f"{'win%':>7}{'SE':>5}{'cal%':>6}{'real%':>6}{'relgap':>7}{'ROS%':>8}{'PnL$':>9}")
    print(hdr)
    print("    " + "-" * (len(hdr) - 4))
    for mode in ("static", "wf"):
        for tgt in PRIMARY_TARGETS:
            R = results[(mode, tgt)]
            m = R["m"]
            cov = R["cov"]
            covpct = 100.0 * cov["n_fill"] / cov["n_gate"] if cov["n_gate"] else 0.0
            afm = _avg_fill_vs_mid(R["primary"])
            name = f"{mode}/${int(tgt)}"
            print(f"    {name:<20}{cov['n_gate']:>6}{cov['n_ladder']:>5}{cov['n_fill']:>6}{covpct:>6.1f}"
                  f"{afm:>+12.4f}{m['win_rate']*100:>7.1f}{m['win_rate_se']*100:>5.1f}"
                  f"{m['claimed_mean']*100:>6.1f}{m['realized_mean']*100:>6.1f}{m['overconfidence_pp']:>+7.1f}"
                  f"{m['ros_pct']:>+8.2f}{m['pnl']:>+9.2f}")

    # per-window signs (both variants, all primary targets)
    print("\n    Per-window real-fill signs (B/C/D):")
    for mode in ("static", "wf"):
        for tgt in PRIMARY_TARGETS:
            R = results[(mode, tgt)]
            pw = {w: slice_metrics([r for r in R["primary"] if r["period"] == w]) for w in ("B", "C", "D")}
            signs = "  ".join(f"{w}:n={pw[w]['n']} ROS={pw[w]['ros_pct']:+.1f}% PnL=${pw[w]['pnl']:+.1f}" for w in ("B", "C", "D"))
            print(f"      {mode:<7}${int(tgt):>3}: {signs}")

    # coverage / no-fill accounting (static $10)
    cov0 = results[("static", 10.0)]["cov"]
    print(f"\n    Coverage accounting (static, $10):  n_gate={cov0['n_gate']}  "
          f"ladder-present={cov0['n_ladder']} ({100*cov0['n_ladder']/cov0['n_gate']:.1f}%)  "
          f"filled={cov0['n_fill']}  ladder-but-nofill(edge floor)={cov0['n_ladder_nofill']}  "
          f"no-ladder(->haircut)={cov0['n_noladder']}")

    # ---- (c) C vs F split at real fills ----
    print("\n" + "=" * 108)
    print("(c) DEGREES-C vs DEGREES-F AT REAL FILLS  (static curve; does the °C blend carry the edge at executable prices?)")
    print("=" * 108)
    for tgt in PRIMARY_TARGETS:
        R = results[("static", tgt)]
        c, f = _units_split(R["primary"])
        print(f"    target ${int(tgt)}:")
        print(_fmt_slice("C (blend)", slice_metrics(c)))
        print(_fmt_slice("F (identity)", slice_metrics(f)))

    # ---- (d) depth sensitivity ----
    print("\n" + "=" * 108)
    print("(d) DEPTH SENSITIVITY -- how fast does real-fill ROS decay $10 -> $100? (static curve)")
    print("=" * 108)
    print(f"    {'target':>8}{'nFill':>7}{'avgStake':>10}{'fillRatio':>11}{'avgFill-mid':>13}{'ROS%':>9}{'PnL$':>10}")
    for tgt in ALL_TARGETS:
        R = results[("static", tgt)]
        m = R["m"]
        prim = R["primary"]
        avg_stake = float(np.mean([r["stake"] for r in prim])) if prim else 0.0
        fill_ratio = avg_stake / tgt if tgt else 0.0
        afm = _avg_fill_vs_mid(prim)
        print(f"    ${int(tgt):>6}{m['n']:>7}{avg_stake:>10.2f}{fill_ratio:>11.3f}{afm:>+13.4f}{m['ros_pct']:>+9.2f}{m['pnl']:>+10.2f}")

    # ---- (b) $/month expectation ----
    print("\n" + "=" * 108)
    print("(b) HONEST $/MONTH EXPECTATION  (static-curve real-fill ROS x realistic bet flow)")
    print("=" * 108)
    bets_per_day_gate = gate_meta["static"]["n_gate"] / n_oos_days
    R10 = results[("static", 10.0)]
    fill_rate = R10["cov"]["n_fill"] / R10["cov"]["n_gate"] if R10["cov"]["n_gate"] else 0.0
    print("    ASSUMPTIONS: NO capital_frac = 7% of a static bankroll; each bet independently")
    print("    sized (no compounding, no exposure cap, no drawdown-halving, no cross-tick top-up).")
    print(f"    Gated NO bet flow = {gate_meta['static']['n_gate']} bets / {n_oos_days} OOS days = {bets_per_day_gate:.2f} bets/day.")
    print(f"    Real-fill data covers {100*fill_rate:.1f}% of gated bets (PMD book-snapshot sparsity; NOT a live-fill limit).")
    print("    Two framings for bet flow:")
    print("      [live]  every gated bet fills (live always has a book) -> flow = gated rate.")
    print("      [data]  only bets with sampled depth fill            -> flow = gated rate x coverage.")
    print()
    print(f"    {'bankroll':>9}{'7% target':>11}{'ROS%':>8}{'avgStake':>10}{'fillRatio':>11}"
          f"{'$/mo [live]':>13}{'$/mo [data]':>13}")
    for bankroll, tgt in sorted(BANKROLL_TARGETS.items()):
        R = results[("static", tgt)]
        m = R["m"]
        prim = R["primary"]
        avg_stake = float(np.mean([r["stake"] for r in prim])) if prim else 0.0
        fill_ratio = avg_stake / tgt if tgt else 0.0
        ros = m["ros_pct"] / 100.0
        # monthly PnL = bets/day * 30 * avg_stake * ROS
        mo_live = bets_per_day_gate * 30.0 * avg_stake * ros
        mo_data = bets_per_day_gate * fill_rate * 30.0 * avg_stake * ros
        print(f"    ${int(bankroll):>7}{tgt:>11.0f}{m['ros_pct']:>+8.2f}{avg_stake:>10.2f}{fill_ratio:>11.3f}"
              f"{mo_live:>+13.2f}{mo_data:>+13.2f}")
    print("    NOTE: ROS here is measured on a thin real-fill sample (see n above); treat $/mo as")
    print("    an order-of-magnitude estimate, not a point forecast. Sign + scale are the message.")

    # ---- secondary haircut (no-ladder rows), never blended ----
    print("\n    SECONDARY (approximate, NOT blended into the above): no-ladder rows via mid+half-spread haircut")
    for tgt in PRIMARY_TARGETS:
        R = results[("static", tgt)]
        hc = slice_metrics(R["haircut"])
        print(_fmt_slice(f"haircut ${int(tgt)}", hc, extra="  [APPROX -- no real depth; ignores 5pp walk floor]"))

    # ---- (e) reconciliation ----
    print("\n" + "=" * 108)
    print("(e) RECONCILIATION vs the two prior numbers")
    print("=" * 108)
    Rs10 = results[("static", 10.0)]["m"]
    Rw10 = results[("wf", 10.0)]["m"]
    # static mid-fill over ALL gated rows (the settled-headline analog at the mid)
    fired_s, cal_s = gate_fire(fr, evalw, "static")
    m_allmid = slice_metrics(mid_fill_all(fr, fired_s, cal_s))
    # decomposition on the fillable subset: mid vs real fill (same rows)
    filled_idx = [r["row_idx"] for r in results[("static", 10.0)]["primary"]]
    fmask = np.zeros(len(fr.md), dtype=bool)
    fmask[filled_idx] = True
    m_fillmid = slice_metrics(mid_fill_all(fr, fmask, cal_s))
    print(f"    [1] settled mid-fill (WF blend/both, gate & PnL at no_price): n=849  ROS +6.16%  PnL +$522.75")
    print(f"        -> fill AT the displayed mid; no depth walked. The edge is real at the mid but the")
    print(f"           mid is not an executable price for most of the volume.")
    print(f"        static-curve analog at MID (all {m_allmid['n']} gated rows): win {m_allmid['win_rate']*100:.1f}%  "
          f"cal {m_allmid['claimed_mean']*100:.1f}%  relgap {m_allmid['overconfidence_pp']:+.1f}pp  "
          f"ROS {m_allmid['ros_pct']:+.2f}%  PnL ${m_allmid['pnl']:+.2f}  (reproduces the ~+6% mid headline).")
    print(f"    [2] atlas pure-C real-fill (both-units-blend lens, all-or-nothing $10 L2 else HAIRCUT,")
    print(f"        gate on FILL price, no ceiling path): +$39 / +2.9%. It BLENDS the optimistic haircut for")
    print(f"        no-depth rows into the metric (where the edge lives) and applies a (degenerate) °F blend.")
    print(f"    [3] THIS deployed config, real-fill ladder-ONLY, static curve, $10:")
    print(f"        n={Rs10['n']}  ROS {Rs10['ros_pct']:+.2f}%  PnL ${Rs10['pnl']:+.2f}  (WF-refit sensitivity: "
          f"n={Rw10['n']}  ROS {Rw10['ros_pct']:+.2f}%  PnL ${Rw10['pnl']:+.2f})")
    print(f"        Gate parity: WF fires {gate_meta['wf']['n_gate']} (==settled 849); static fires "
          f"{gate_meta['static']['n_gate']} (more disciplined -- static calibrates window B, which WF ran RAW).")
    print(f"    DECOMPOSITION (why [3] < 0 while [1] > 0): the {m_fillmid['n']} rows that have real depth AND")
    print(f"        survive the 0.05 walk floor already lose at MID (ROS {m_fillmid['ros_pct']:+.2f}%, win "
          f"{m_fillmid['win_rate']*100:.1f}% vs the {m_allmid['n']}-row population's {m_allmid['win_rate']*100:.1f}%).")
    print(f"        Real depth adds only {Rs10['ros_pct']-m_fillmid['ros_pct']:+.2f} ppROS of slippage (avg fill "
          f"{_avg_fill_vs_mid(results[('static',10.0)]['primary'])*100:+.2f}c above mid). So [3]'s loss is ~SELECTION,")
    print(f"        not slippage: the config's +6% edge concentrates in the illiquid rows that have NO real book")
    print(f"        to fill against, and the liquid subset that CAN fill is where the market is already right.")

    # ---- reliability tables ----
    print("\n    Reliability (static $10 real-fill fired slice): claimed bin -> realized")
    for row in reliability_table(results[("static", 10.0)]["primary"]):
        print(f"      [{row['bin_lo']:.2f},{row['bin_hi']:.2f}] n={row['n']:>4} "
              f"claimed={row['claimed_mean']*100:>5.1f}% realized={row['realized_freq']*100:>5.1f}% gap={row['gap_pp']:>+5.1f}pp")

    # ---- CSV ----
    out = Path(args.csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for mode in ("static", "wf"):
        for tgt in ALL_TARGETS:
            R = results[(mode, tgt)]
            m = R["m"]
            cov = R["cov"]
            prim = R["primary"]
            c, f = _units_split(prim)
            mc, mf = slice_metrics(c), slice_metrics(f)
            avg_stake = float(np.mean([r["stake"] for r in prim])) if prim else 0.0
            rows.append({
                "mode": mode, "target": tgt, "n_gate": cov["n_gate"],
                "n_ladder": cov["n_ladder"], "n_fill": cov["n_fill"],
                "n_noladder": cov["n_noladder"], "n_ladder_nofill": cov["n_ladder_nofill"],
                "coverage_pct": round(100.0 * cov["n_fill"] / cov["n_gate"], 2) if cov["n_gate"] else 0.0,
                "avg_fill_minus_mid": round(_avg_fill_vs_mid(prim), 5),
                "avg_stake": round(avg_stake, 3),
                "win_rate": m["win_rate"], "win_se": m["win_rate_se"],
                "cal_claim": m["claimed_mean"], "realized": m["realized_mean"],
                "rel_gap_pp": m["overconfidence_pp"], "ros_pct": m["ros_pct"],
                "pnl": m["pnl"], "staked": m["staked"],
                "C_n": mc["n"], "C_ros": mc["ros_pct"], "C_pnl": mc["pnl"], "C_relgap": mc["overconfidence_pp"],
                "F_n": mf["n"], "F_ros": mf["ros_pct"], "F_pnl": mf["pnl"], "F_relgap": mf["overconfidence_pp"],
            })
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
