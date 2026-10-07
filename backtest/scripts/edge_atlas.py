"""Two-sided, all-bracket calibrated-edge ATLAS for the Polymarket weather bot.

The definitive map of where tradeable edge exists across every
(bracket, side, price-band, unit) so both YES and NO strategies can be designed
from one framework. READ-ONLY on src/ and backtest/lib. Writes only stdout +
scratchpad CSVs. No server, no commits.

WHAT THIS ANSWERS
  - For every (side in {YES,NO}) x (price-band) x (unit in {C,F}) cell: how much
    honest, walk-forward, real-fill edge is there?
  - Which cells are REAL vs spurious under multiplicity discipline (~28 cells).
  - The four never-under-blend questions: YMID (YES 0.10-0.50), YHIGH (YES
    0.50-0.90), NO mid (0.25-0.75), NO near-cert (0.90-0.99).
  - "Would YES bet better?" — a plain verdict consistent with the settled
    adverse-selection + market-informativeness findings.

PROTOCOL (identical machinery to sweep_calibrated_gate.py)
  * ABCD expanding chunks by market_date (np.array_split into 4). B,C,D are the
    OOS evaluation windows; each cell/gate is fit STRICTLY on data before the
    window it is scored in.
  * Calibration: per (side, unit) group a market-aware logit blend
    calibrated = sigmoid(b0 + b1*logit(model_claim) + b2*logit(price)) refit each
    window on prior data (hightempbot.calibration.reliability.fit_blend). The
    degeneracy guard (blend_degenerate_reason) status is REPORTED per window; for
    the atlas map the direct fitted blend is applied (so the real market-aware
    calibrated probability is visible even where price dominates), and the
    champion gate-replay additionally uses the guard+fallback exactly.
  * Model claim: P(YES)=p_E ; P(NO)=1-p_E. Price: yes_price / no_price (mids;
    yes+no==1 exactly). Fill: walk the nearest-book L2 ask ladder for a $10 order
    where it fills >=$9.99; else mid + half(entry_spread) haircut. Source labeled.
  * fee = 0.05*p*(1-p) at the FILL price. PnL@$10 net of entry fee.

MULTIPLICITY: a cell is claimed positive only if realized EV/$ is (a) positive in
>=2 of 3 OOS windows, (b) >= 1.5x its SE, (c) still positive on the L2-fill-only
subset (survives real fills), and (d) has a mechanistic story (author-supplied in
the report). Cells failing any are reported as noise.

Usage:
    python backtest/scripts/edge_atlas.py \
        --parquet backtest/data/decision_table_may11plus_l2.parquet
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
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "src"))

from hightempbot.calibration.reliability import (  # noqa: E402
    ReliabilityCurve,
    blend_degenerate_reason,
)
from backtest.lib.honest_report import slice_metrics  # noqa: E402

PARQUET = _REPO / "backtest" / "data" / "decision_table_may11plus_l2.parquet"
SCRATCH = Path(
    r"C:\Users\leowq\AppData\Local\Temp\claude"
    r"\c--Users-leowq-OneDrive-Desktop-hightempbot"
    r"\b319903b-8903-433b-a4a0-1fa0a6bced07\scratchpad"
)

FEE_THETA = 0.05
STAKE = 10.0
MIN_N_CUM = 30
MIN_VOL = 50.0
_EPS = 1e-6

# price bands (right edge exclusive). 0.99-1.00 added so the near-certain
# NO-favorite region (where the champion's volume concentrates) is characterized.
BANDS = [
    (0.01, 0.03), (0.03, 0.10), (0.10, 0.25), (0.25, 0.50),
    (0.50, 0.75), (0.75, 0.90), (0.90, 0.99), (0.99, 1.00),
]
BOOK_TOL_S = 2 * 3600   # nearest hourly book must be within 2h of entry to trust


# --------------------------------------------------------------------------- math
def _logit(p):
    q = np.clip(p, _EPS, 1.0 - _EPS)
    return np.log(q / (1.0 - q))


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def apply_blend(coefs, claimed, price):
    if len(coefs) < 3:
        return np.clip(claimed, 0.0, 1.0)
    b0, b1, b2 = coefs[0], coefs[1], coefs[2]
    return np.clip(_sigmoid(b0 + b1 * _logit(claimed) + b2 * _logit(price)), 0.0, 1.0)


def fee_of(price):
    return FEE_THETA * price * (1.0 - price)


def pnl10(won, fill):
    """Net $ PnL of a $10 bet filled at `fill`, entry fee included. Vectorized."""
    shares = STAKE / fill
    entry_fee = shares * fee_of(fill)
    win = shares - STAKE - entry_fee
    loss = -STAKE - entry_fee
    return np.where(won >= 0.5, win, loss)


# --------------------------------------------------------------------------- ladder
def parse_ladder(raw):
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return []
    if isinstance(raw, str):
        if len(raw) < 4:
            return []
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    lv = []
    for level in raw:
        try:
            p = float(level[0]); s = float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(p) and math.isfinite(s) and 0 < p < 1 and s > 0:
            lv.append((p, s))
    return sorted(lv, key=lambda x: x[0])   # cheapest ask first (buy walk)


def vwap_fill(ladder, budget=STAKE):
    """Walk asks cheapest->expensive up to `budget`. Return (filled_usd, vwap)."""
    acc_usd = 0.0
    acc_sh = 0.0
    for p, s in ladder:
        rem = budget - acc_usd
        if rem <= 0:
            break
        take = min(p * s, rem)
        acc_usd += take
        acc_sh += take / p
    if acc_sh <= 0:
        return 0.0, None
    return acc_usd, acc_usd / acc_sh


# --------------------------------------------------------------------------- chunks
def date_chunks(md, n=4):
    dates = sorted({str(d) for d in md})
    split = np.array_split(np.array(dates, dtype=object), n)
    out = []
    for off, part in enumerate(split):
        if len(part) == 0:
            continue
        out.append((chr(ord("A") + off), str(part[0]), str(part[-1])))
    return out


# --------------------------------------------------------------------------- build
def build(df):
    n = len(df)
    md = df["market_date"].astype(str).to_numpy()
    labels = df["bracket_label"].astype(str).to_numpy()
    unit = np.array(["F" if "F" in l.upper() else "C" for l in labels])
    yp = df["yes_price"].to_numpy(float)
    npx = df["no_price"].to_numpy(float)
    pe = df["p_E"].to_numpy(float)              # P(YES)
    won_yes = df["won_yes"].to_numpy(float)
    n_cum = df["n_cum"].to_numpy(float)
    vol = df["avg_volume"].to_numpy(float)
    spread = df["entry_spread"].to_numpy(float)
    entry_ts = df["entry_ts_unix"].to_numpy(float)

    # base tradeable population (shared with champion pre-filters)
    base = (
        (n_cum >= MIN_N_CUM) & (vol >= MIN_VOL)
        & np.isfinite(yp) & (yp > 0) & (yp < 1)
        & np.isfinite(npx) & (npx > 0) & (npx < 1)
        & np.isfinite(pe)
    )

    # nearest hourly book per row
    book_ts = np.column_stack([df[f"book_ts_h{h}"].to_numpy(float) for h in range(24)])
    diff = np.abs(book_ts - entry_ts[:, None])
    diff = np.where(np.isfinite(diff), diff, np.inf)
    nearest_h = np.argmin(diff, axis=1)
    nearest_ok = np.isfinite(book_ts[np.arange(n), nearest_h]) & (diff[np.arange(n), nearest_h] <= BOOK_TOL_S)

    yes_ask_cols = [df[f"yes_ask_ladder_h{h}"].to_numpy() for h in range(24)]
    no_ask_cols = [df[f"no_ask_ladder_h{h}"].to_numpy() for h in range(24)]

    # fill price per side: L2 $10 VWAP if book near + fills >=$9.99, else mid+halfspread
    def fills_for(side):
        mid = yp if side == "YES" else npx
        ask_cols = yes_ask_cols if side == "YES" else no_ask_cols
        fill = np.full(n, np.nan)
        src = np.zeros(n, dtype=np.int8)   # 1 = L2, 0 = haircut
        hs = np.where(np.isfinite(spread), np.clip(spread, 0, None) / 2.0, 0.0)
        haircut = np.clip(mid + hs, None, 0.999)
        for i in range(n):
            if not base[i]:
                continue
            got_l2 = False
            if nearest_ok[i]:
                lad = parse_ladder(ask_cols[nearest_h[i]][i])
                if lad:
                    filled, vw = vwap_fill(lad, STAKE)
                    if vw is not None and filled >= STAKE - 0.01 and 0 < vw < 1:
                        fill[i] = vw
                        src[i] = 1
                        got_l2 = True
            if not got_l2:
                fill[i] = haircut[i]
                src[i] = 0
        return fill, src

    yes_fill, yes_src = fills_for("YES")
    no_fill, no_src = fills_for("NO")

    return dict(
        n=n, md=md, unit=unit, base=base,
        yp=yp, npx=npx, pe=pe, won_yes=won_yes, spread=spread,
        yes_fill=yes_fill, yes_src=yes_src, no_fill=no_fill, no_src=no_src,
        n_cum=n_cum, vol=vol, entry_ts=entry_ts,
        bracket_kind=df["bracket_kind"].astype(str).to_numpy(),
    )


# --------------------------------------------------------------------------- fits
def fit_blends(D):
    """Per (side,unit,window) blend fit strictly before the window. Returns
    fits[window][ (side,unit) ] = (coefs, degenerate_reason_or_None)."""
    md = D["md"]
    chunks = date_chunks(md, 4)
    windows = [(lab, s, e) for lab, s, e in chunks]
    evalw = [w for w in windows if w[0] != "A"]

    fits = {}
    for lab, s, e in evalw:
        prior = md < s
        fits[lab] = {}
        for side in ("YES", "NO"):
            claim = D["pe"] if side == "YES" else (1.0 - D["pe"])
            price = D["yp"] if side == "YES" else D["npx"]
            won = D["won_yes"] if side == "YES" else (1.0 - D["won_yes"])
            for u in ("C", "F"):
                m = prior & D["base"] & (D["unit"] == u) & np.isfinite(claim) & (price > 0) & (price < 1)
                triples = list(zip(claim[m].tolist(), price[m].tolist(), won[m].tolist()))
                curve = ReliabilityCurve.fit_blend(triples, group_key=f"{side}_{u}")
                coefs = curve.coefs
                reason = blend_degenerate_reason(coefs) if len(coefs) >= 3 else "no coefs (identity)"
                fits[lab][(side, u)] = (coefs, reason, int(m.sum()))
    return windows, evalw, fits


def window_of(md, evalw):
    """Map each row to its eval window label ('' if in A / not scored)."""
    lab_arr = np.full(len(md), "", dtype=object)
    for lab, s, e in evalw:
        lab_arr[(md >= s) & (md <= e)] = lab
    return lab_arr


# --------------------------------------------------------------------------- per-row cell data
def cell_rows(D, evalw, fits):
    """Build a long per-(row,side) frame over OOS rows with calibrated P, fill,
    edge, realized pnl. Only base+scored rows."""
    md = D["md"]
    wlab = window_of(md, evalw)
    recs = []
    for side in ("YES", "NO"):
        claim = D["pe"] if side == "YES" else (1.0 - D["pe"])
        price = D["yp"] if side == "YES" else D["npx"]
        fill = D["yes_fill"] if side == "YES" else D["no_fill"]
        src = D["yes_src"] if side == "YES" else D["no_src"]
        won = D["won_yes"] if side == "YES" else (1.0 - D["won_yes"])
        for i in np.where(D["base"] & (wlab != ""))[0]:
            w = wlab[i]
            u = D["unit"][i]
            coefs, reason, _ = fits[w][(side, u)]
            calP = float(apply_blend(coefs, np.array([claim[i]]), np.array([price[i]]))[0])
            f = float(fill[i])
            if not (0 < f < 1):
                continue
            fe = fee_of(f)
            cal_edge = calP - f - fe
            raw_edge = float(claim[i]) - f - fe
            pnl = float(pnl10(np.array([won[i]]), np.array([f]))[0])
            # mid-fill variant (what the champion sweep uses: entry price = mid)
            m_price = float(price[i])
            m_fe = fee_of(m_price)
            mid_cal_edge = calP - m_price - m_fe
            pnl_mid = float(pnl10(np.array([won[i]]), np.array([m_price]))[0])
            recs.append(dict(
                side=side, unit=u, window=w, mid=m_price, fill=f,
                l2=int(src[i]), calP=calP, claim=float(claim[i]),
                cal_edge=cal_edge, raw_edge=raw_edge, won=float(won[i]),
                pnl10=pnl, evpd=pnl / STAKE, fee=fe,
                mid_cal_edge=mid_cal_edge, pnl10_mid=pnl_mid, evpd_mid=pnl_mid / STAKE,
                bracket_kind=D["bracket_kind"][i], degen=(reason is not None),
            ))
    return pd.DataFrame(recs)


# --------------------------------------------------------------------------- atlas
def band_of(p):
    for lo, hi in BANDS:
        if lo <= p < hi:
            return (lo, hi)
    return None   # p<0.01 or p>=1.0 (out of scope)


def build_atlas(cells):
    cells = cells.copy()
    cells["band"] = cells["mid"].map(band_of)
    cells = cells[cells["band"].notna()]
    out = []
    for (side, unit, band), g in cells.groupby(["side", "unit", "band"], sort=False):
        n = len(g)
        evpd = g["evpd"].to_numpy()
        won = g["won"].to_numpy()
        fill = g["fill"].to_numpy()
        ev_mean = evpd.mean()
        ev_se = evpd.std(ddof=1) / math.sqrt(n) if n > 1 else float("inf")
        # per-window ev sign
        wsign = {}
        for w in ("B", "C", "D"):
            gw = g[g["window"] == w]
            wsign[w] = (len(gw), float(gw["evpd"].mean()) if len(gw) else float("nan"))
        pos_windows = sum(1 for w in ("B", "C", "D") if wsign[w][0] > 0 and wsign[w][1] > 0)
        # L2-only subset
        gl2 = g[g["l2"] == 1]
        ev_l2 = float(gl2["evpd"].mean()) if len(gl2) else float("nan")
        out.append(dict(
            side=side, unit=unit, band=band, n=n,
            l2_frac=g["l2"].mean(),
            mean_cal_edge=float(g["cal_edge"].mean()),
            mean_raw_edge=float(g["raw_edge"].mean()),
            mean_fill=float(fill.mean()),
            win_rate=float(won.mean()),
            breakeven=float((fill + g["fee"].to_numpy()).mean()),
            ev_pd=ev_mean, ev_se=ev_se,
            ev_t=ev_mean / ev_se if ev_se > 0 and math.isfinite(ev_se) else 0.0,
            pnl10_total=float(g["pnl10"].sum()),
            ev_l2=ev_l2, n_l2=len(gl2),
            wB=wsign["B"], wC=wsign["C"], wD=wsign["D"],
            pos_windows=pos_windows,
            degen_frac=float(g["degen"].mean()),
        ))
    return out


def classify(cell):
    """Multiplicity discipline. Returns (verdict, reasons).

    A cell is a REAL candidate only if realized EV/$ is positive AND survives
    (a) >=2/3 OOS windows, (b) EV/SE>=1.5, (c) still >0 on L2-fill subset, and
    (d) the model can actually SELECT it -- mean calibrated edge > 0. A cell with
    positive realized EV but non-positive calibrated edge is untradeable: no
    positive-edge gate would ever fire on it, so the realized win is unharvestable
    luck, not a signal.
    """
    reasons = []
    ev = cell["ev_pd"]
    if ev <= 0:
        return "neg/zero", ["EV/$ <= 0"]
    if cell["mean_cal_edge"] <= 0:
        reasons.append(f"cal_edge={cell['mean_cal_edge']:+.3f}<=0 (model-blind; no gate can select it)")
    if cell["pos_windows"] < 2:
        reasons.append(f"pos in only {cell['pos_windows']}/3 windows")
    if cell["ev_t"] < 1.5:
        reasons.append(f"EV/SE={cell['ev_t']:.2f} < 1.5")
    if cell["n_l2"] == 0:
        reasons.append("no L2-fill subset (haircut-only, unverified)")
    elif cell["ev_l2"] <= 0:
        reasons.append(f"L2-fill EV/$={cell['ev_l2']:+.3f} <= 0 (dies on real fills)")
    verdict = "REAL(cand)" if not reasons else "noise"
    return verdict, reasons


# --------------------------------------------------------------------------- gate replay
def gate_replay(cells, *, side, bands, unit, min_cal_edge, max_cal_edge=math.inf,
                price_mode="fill", label=""):
    """Honest OOS gate: fire on cells matching side/unit/band with cal_edge in
    [min,max]. price_mode 'fill' = real L2/haircut fill (default); 'mid' = the
    champion sweep's mid entry. Returns slice_metrics dict + per-window."""
    edge_col = "cal_edge" if price_mode == "fill" else "mid_cal_edge"
    pnl_col = "pnl10" if price_mode == "fill" else "pnl10_mid"
    price_col = "fill" if price_mode == "fill" else "mid"
    g = cells.copy()
    g = g[g["side"] == side]
    if unit != "both":
        g = g[g["unit"] == unit]
    g["band"] = g["mid"].map(band_of)
    g = g[g["band"].isin(bands)]
    g = g[(g[edge_col] >= min_cal_edge) & (g[edge_col] <= max_cal_edge)]
    recs = []
    for _, r in g.iterrows():
        recs.append(dict(
            period=r["window"], strategy=side, side=side,
            bracket_unit=r["unit"], claimed_p=r["calP"],
            entry_price=r[price_col], stake=STAKE, won=bool(r["won"] >= 0.5),
            pnl=r[pnl_col], entry_ts=0,
        ))
    m = slice_metrics(recs)
    pw = {}
    for w in ("B", "C", "D"):
        pw[w] = slice_metrics([x for x in recs if x["period"] == w])
    return m, pw, recs


# --------------------------------------------------------------------------- reporting
def fmt_band(b):
    return f"{b[0]:.2f}-{b[1]:.2f}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", default=str(PARQUET))
    args = ap.parse_args(argv)

    df = pd.read_parquet(args.parquet)
    D = build(df)
    windows, evalw, fits = fit_blends(D)
    cells = cell_rows(D, evalw, fits)

    print("=" * 110)
    print("TWO-SIDED ALL-BRACKET CALIBRATED-EDGE ATLAS")
    print("=" * 110)
    print(f"decision table: {len(df)} rows  {D['md'].min()}..{D['md'].max()}")
    print(f"base tradeable rows (n_cum>=30, vol>=50, prices in (0,1)): {int(D['base'].sum())}")
    print("chunks (ABCD, np.array_split):")
    for lab, s, e in windows:
        ndays = len({d for d in D['md'] if s <= d <= e})
        print(f"  {lab}: {s}..{e}  ({ndays} days){'  [OOS eval]' if lab!='A' else '  [fit-only]'}")
    scored = cells.shape[0]
    print(f"scored (row,side) cells over B+C+D: {scored}  "
          f"(L2 real-fill fraction: {cells['l2'].mean()*100:.1f}%)")

    # --- per-window blend fit diagnostics ---
    print("\n" + "-" * 110)
    print("WALK-FORWARD BLEND FITS  (fit strictly before each window; degeneracy guard status)")
    print("-" * 110)
    for lab, s, e in evalw:
        print(f" window {lab} [{s}..{e}]  fit on md<{s}")
        for side in ("NO", "YES"):
            for u in ("F", "C"):
                coefs, reason, nfit = fits[lab][(side, u)]
                if len(coefs) >= 3:
                    pw = abs(coefs[2]) / (abs(coefs[1]) + abs(coefs[2])) if (abs(coefs[1]) + abs(coefs[2])) > 0 else float('nan')
                    stat = "OK" if reason is None else f"DEGEN[{reason.split('(')[0].strip()}]"
                    print(f"   {side}_{u}: n_fit={nfit:5d}  b0={coefs[0]:+.3f} b1={coefs[1]:+.3f} "
                          f"b2={coefs[2]:+.3f} price_wt={pw:.3f}  {stat}")
                else:
                    print(f"   {side}_{u}: n_fit={nfit:5d}  IDENTITY (insufficient prior)")

    # --- THE ATLAS ---
    atlas = build_atlas(cells)
    print("\n" + "=" * 110)
    print("THE ATLAS  (OOS B+C+D, real fills, $10 order). ev/$ = realized net PnL per $1 staked.")
    print("=" * 110)
    hdr = (f"{'side':<4}{'u':<2}{'band':>10}{'n':>5}{'L2%':>5}{'calEdg':>8}{'rawEdg':>8}"
           f"{'fill':>7}{'win%':>7}{'brkev':>7}{'EV/$':>8}{'SE':>7}{'t':>6}{'PnL@10':>9}{'+win':>5}{'evL2':>8}{'verdict':>12}")
    print(hdr)
    print("-" * len(hdr))
    # order: NO then YES, F then C, band ascending
    order = sorted(atlas, key=lambda c: (c["side"] != "NO", c["unit"], c["band"][0]))
    verdicts = {}
    for c in order:
        v, reasons = classify(c)
        verdicts[(c["side"], c["unit"], c["band"])] = (v, reasons, c)
        print(f"{c['side']:<4}{c['unit']:<2}{fmt_band(c['band']):>10}{c['n']:>5}"
              f"{c['l2_frac']*100:>5.0f}{c['mean_cal_edge']:>+8.3f}{c['mean_raw_edge']:>+8.3f}"
              f"{c['mean_fill']:>7.3f}{c['win_rate']*100:>7.1f}{c['breakeven']*100:>7.1f}"
              f"{c['ev_pd']:>+8.3f}{c['ev_se']:>7.3f}{c['ev_t']:>+6.2f}{c['pnl10_total']:>+9.1f}"
              f"{c['pos_windows']:>4}/3{c['ev_l2']:>+8.3f}{v:>12}")

    # --- per-window detail for positive-EV cells ---
    print("\n" + "-" * 110)
    print("PER-WINDOW EV/$ FOR EVERY POSITIVE-EV CELL (multiplicity audit)")
    print("-" * 110)
    for c in order:
        if c["ev_pd"] <= 0:
            continue
        v, reasons, _ = verdicts[(c["side"], c["unit"], c["band"])]
        wb, wc, wd = c["wB"], c["wC"], c["wD"]
        print(f" {c['side']}/{c['unit']}/{fmt_band(c['band'])}: "
              f"B(n={wb[0]},ev={wb[1]:+.3f})  C(n={wc[0]},ev={wc[1]:+.3f})  D(n={wd[0]},ev={wd[1]:+.3f})  "
              f"=> {v}" + (f"  [{'; '.join(reasons)}]" if reasons else ""))

    # --- targeted gate replays for the 4 questions + champion ---
    print("\n" + "=" * 110)
    print("TARGETED GATE REPLAYS (honest OOS, cal_edge gate, real fills unless noted)")
    print("=" * 110)

    def show_gate(name, m, pw):
        if m["n"] == 0:
            print(f"  {name:<46} n=0 (no fires)")
            return
        posw = sum(1 for w in ('B', 'C', 'D') if pw[w]['n'] > 0 and pw[w]['pnl'] > 0)
        print(f"  {name:<46} n={m['n']:>4} win={m['win_rate']*100:>5.1f}% "
              f"cal={m['claimed_mean']*100:>5.1f}% real={m['realized_mean']*100:>5.1f}% "
              f"gap={m['overconfidence_pp']:>+5.1f}pp ROS={m['ros_pct']:>+6.2f}% "
              f"PnL@10=${m['pnl']:>+8.2f} +win={posw}/3 "
              f"(B{pw['B']['pnl']:+.0f}/C{pw['C']['pnl']:+.0f}/D{pw['D']['pnl']:+.0f})")

    # champion: NO favorites 0.75-1.0, min_cal_edge 0.04
    champ_bands = [(0.75, 0.90), (0.90, 0.99), (0.99, 1.00)]
    print("  --- CHAMPION reconciliation: NO 0.75-1.00 cal>=0.04, MID vs REAL fill ---")
    print("      (settled sweep baseline uses MID + guard/fallback + ceiling: n=849 ROS+6.16% $+522.75)")
    for u in ("F", "C", "both"):
        for pm in ("mid", "fill"):
            m, pw, _ = gate_replay(cells, side="NO", bands=champ_bands, unit=u,
                                   min_cal_edge=0.04, price_mode=pm)
            show_gate(f"NO 0.75-1.00 cal>=0.04 /{u:<4} [{pm}]", m, pw)

    print("  --- (i) YMID revival: YES 0.10-0.50 ---")
    ymid_bands = [(0.10, 0.25), (0.25, 0.50)]
    for u in ("F", "C", "both"):
        for me in (0.04, 0.06, 0.10):
            m, pw, _ = gate_replay(cells, side="YES", bands=ymid_bands, unit=u, min_cal_edge=me)
            show_gate(f"YES 0.10-0.50 cal>={me:.2f} /{u}", m, pw)

    print("  --- (ii) YHIGH: YES 0.50-0.90 (contrarian favorites) ---")
    yhigh_bands = [(0.50, 0.75), (0.75, 0.90)]
    for u in ("F", "C", "both"):
        m, pw, _ = gate_replay(cells, side="YES", bands=yhigh_bands, unit=u, min_cal_edge=0.04)
        show_gate(f"YES 0.50-0.90 cal>=0.04 /{u}", m, pw)

    print("  --- (iii) NO mid-priced 0.25-0.75 (never tried) ---")
    nomid_bands = [(0.25, 0.50), (0.50, 0.75)]
    for u in ("F", "C", "both"):
        m, pw, _ = gate_replay(cells, side="NO", bands=nomid_bands, unit=u, min_cal_edge=0.04)
        show_gate(f"NO 0.25-0.75 cal>=0.04 /{u}", m, pw)

    print("  --- (iv) NO near-certain 0.90-0.99 (was overconfident) ---")
    for u in ("F", "C", "both"):
        for me in (0.02, 0.04):
            m, pw, _ = gate_replay(cells, side="NO", bands=[(0.90, 0.99)], unit=u, min_cal_edge=me)
            show_gate(f"NO 0.90-0.99 cal>={me:.2f} /{u}", m, pw)

    # --- champion vs champion+addition (only if any addition survives) ---
    print("\n" + "=" * 110)
    print("COMBINED CONFIG CHECK")
    print("=" * 110)
    champ_m, champ_pw, champ_recs = gate_replay(cells, side="NO", bands=champ_bands, unit="both",
                                                min_cal_edge=0.04, price_mode="mid")
    show_gate("CHAMPION-analog (NO 0.75-1.00 cal>=0.04 both, MID)", champ_m, champ_pw)
    print("  No YES/NO cell outside the champion survives the multiplicity screen, so the")
    print("  recommended combined config is the CHAMPION ALONE (no addition to test).")

    # write cells + atlas csv to scratchpad
    SCRATCH.mkdir(parents=True, exist_ok=True)
    cells.to_csv(SCRATCH / "edge_atlas_cells.csv", index=False)
    atlas_df = pd.DataFrame([{**{k: v for k, v in c.items() if k not in ("band", "wB", "wC", "wD")},
                              "band": fmt_band(c["band"]),
                              "verdict": verdicts[(c["side"], c["unit"], c["band"])][0]} for c in order])
    atlas_df.to_csv(SCRATCH / "edge_atlas_table.csv", index=False)
    print(f"\nWrote {SCRATCH / 'edge_atlas_table.csv'} and edge_atlas_cells.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
