"""Data-forensics slice hunt on YES-side TAIL bets.

Question: does ANY sub-population of cheap YES tails have real, walk-forward-honest
positive edge, or is the sleeve EV<=0 everywhere?

Read-only. Reproduces the deployed TAIL vote gate from
backtest/configs/candidate_l2_depth.json (alpha 4.0, 4-of-4 votes, hour 1 local,
consensus_skip 0.40, delayed entry to YES ask <= 2c, fee theta 0.05), then widens
the population for statistical power and slices it every way the orchestrator asked:

  (a) hot vs cold tail (bracket index relative to the model argmax bucket)
  (b) entry hour + hours-to-resolution
  (c) PRICE MOMENTUM into entry (1-min PMD series) -- the adverse-selection flip
  (d) ask-depth at entry (thin vs thick, from the L2 ask ladder)
  (e) model claim level (2-5% / 5-10% / 10-15% / >15%)
  (f) vote dispersion across the 4 signals
  (g) station unit C vs F

Walk-forward honesty: ABCD chronological chunks (repo convention). Slice selection
is done on A+B and scored on C+D. In-sample-only tables are labelled as such.

Nothing here is committed or written outside stdout / the scratchpad.
"""
from __future__ import annotations

import bisect
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

PARQUET = REPO_ROOT / "backtest" / "data" / "decision_table_may11plus_l2.parquet"
PRICE_DB = REPO_ROOT / "backtest" / "data" / "polymarket_history.db"

SIGS = ["p_E", "p_B_50", "p_L_loose", "p_Shrink_n10"]
THETA = 0.05           # Polymarket fee: theta * p * (1-p)
ALPHA = 4.0            # deployed vote multiplier
N_REQUIRED = 4         # deployed 4-of-4
DELAY_FP = 0.02        # deployed delayed-entry ask threshold (2c)
LATEST_ENTRY_BEFORE_CLOSE = 4 * 3600  # entries must be >=4h before bracket close


# --------------------------------------------------------------------------- honest stats

def se_prop(k: int, n: int) -> float:
    if n <= 0:
        return 0.0
    p = k / n
    return math.sqrt(max(p * (1 - p), 0.0) / n)


def ev_per_dollar(win: np.ndarray, price: np.ndarray) -> np.ndarray:
    """Net $ per $1 staked buying YES at `price`, matching the live fee model.

    win : (1-p)/p - theta*(1-p)   ;   lose : -1 - theta*(1-p)
    """
    fee_drag = THETA * (1.0 - price)
    winpay = (1.0 - price) / price - fee_drag
    losepay = -1.0 - fee_drag
    return np.where(win > 0.5, winpay, losepay)


def slice_row(name, win, price, *, pnl_stake=10.0):
    n = len(win)
    if n == 0:
        return dict(name=name, n=0, wins=0, wr=0.0, se=0.0, px=0.0, ev=0.0, pnl=0.0)
    k = int(win.sum())
    ev = ev_per_dollar(win, price)
    return dict(name=name, n=n, wins=k, wr=k / n, se=se_prop(k, n),
                px=float(price.mean()), ev=float(ev.mean()),
                pnl=float(pnl_stake * ev.sum()))


def fmt_row(r, breakeven_note=""):
    if r["n"] == 0:
        return f"  {r['name']:<34} n=   0  (no bets)"
    return (f"  {r['name']:<34} n={r['n']:>4} w={r['wins']:>2} "
            f"wr={r['wr']*100:>5.2f}%+/-{r['se']*100:>4.2f}  "
            f"px={r['px']*100:>5.2f}c  EV/$={r['ev']:>+6.3f}  "
            f"PnL@$10=${r['pnl']:>+8.2f}{breakeven_note}")


def table(title, rows, note=""):
    print(f"\n{title}")
    if note:
        print(f"  ({note})")
    for r in rows:
        print(fmt_row(r))


# --------------------------------------------------------------------------- price series

def load_yes_prices(slugs):
    con = sqlite3.connect(f"file:{PRICE_DB}?mode=ro", uri=True)
    qm = ",".join("?" * len(slugs))
    out: dict[str, list[tuple[int, float]]] = {}
    for slug, ts, price in con.execute(
        f"SELECT market_slug, ts_unix, price FROM prices "
        f"WHERE side='Yes' AND price IS NOT NULL AND market_slug IN ({qm}) "
        f"ORDER BY market_slug, ts_unix",
        tuple(slugs),
    ):
        out.setdefault(slug, []).append((int(ts), float(price)))
    con.close()
    return out


def price_at(series, keys, t):
    i = bisect.bisect_right(keys, t) - 1
    return series[i][1] if i >= 0 else None


def delayed_entry(series, keys, entry_ts, close_ts, thr=DELAY_FP):
    """First post-signal 1-min print at or below `thr`, still >=4h before close."""
    st = bisect.bisect_left(keys, entry_ts)
    latest = close_ts - LATEST_ENTRY_BEFORE_CLOSE
    for ts, p in series[st:]:
        if ts > latest:
            return None
        if p <= thr:
            return ts, p
    return None


def cross_up(series, keys, entry_ts, close_ts, target):
    """First post-entry print >= target (a TP exit needs a print at/above)."""
    st = bisect.bisect_right(keys, entry_ts)
    for ts, p in series[st:]:
        if ts > close_ts:
            break
        if p >= target:
            return ts, p
    return None


# --------------------------------------------------------------------------- ladder depth

def parse_ladder(raw):
    import json
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
            p = float(level[0]); s = float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(p) and math.isfinite(s) and 0 < p < 1 and s > 0:
            lv.append((p, s))
    return sorted(lv, key=lambda x: x[0])


def ask_depth_usd(ladder, max_price):
    """Total $ notional of YES ask resting at price <= max_price."""
    return sum(p * s for p, s in parse_ladder(ladder) if p <= max_price + 1e-9)


def vwap_fill(ladder, budget_usd):
    """Walk the ask ladder cheap->expensive up to budget_usd.

    Returns (filled_usd, realized_vwap). This is the *executable* price for a
    real order, in contrast to the 1-min PMD print (a mid / last trade).
    """
    acc_usd = 0.0
    acc_sh = 0.0
    for p, s in parse_ladder(ladder):
        rem = budget_usd - acc_usd
        if rem <= 0:
            break
        take = min(p * s, rem)
        acc_usd += take
        acc_sh += take / p
    if acc_sh <= 0:
        return 0.0, None
    return acc_usd, acc_usd / acc_sh


# --------------------------------------------------------------------------- build table

def build_candidates(df, hour, *, fp_max=0.05, votes_required=None,
                     consensus=None):
    """Return boolean mask + arrays for cheap-tail candidates at `hour`."""
    N = len(df)
    yp = df[f"yes_price_h{hour}"].to_numpy(float)
    ts = df[f"entry_ts_h{hour}"].to_numpy(float)
    vol = df[f"entry_volume_h{hour}"].fillna(0).to_numpy(float)
    ncum = df["n_cum"].fillna(0).to_numpy(float)
    md = pd.to_datetime(df["market_date"]).astype("datetime64[s]").astype("int64").to_numpy()
    not_leak = ~np.isnan(ts) & (ts >= md)
    votes = np.zeros(N, int)
    any_nan = np.zeros(N, bool)
    pavg = np.zeros(N)
    sig_vals = []
    for s in SIGS:
        ps = df[s].to_numpy(float)
        votes += ((ps >= ALPHA * yp) & ~np.isnan(ps)).astype(int)
        any_nan |= np.isnan(ps)
        pavg += np.nan_to_num(ps)
        sig_vals.append(ps)
    pavg /= len(SIGS)
    disp = np.nanstd(np.vstack(sig_vals), axis=0)
    mask = (ncum >= 30) & (yp >= 0.01) & (yp <= fp_max) & ~any_nan & not_leak & (vol >= 50)
    if votes_required is not None:
        mask &= votes >= votes_required
    if consensus is not None:
        gmax = df.groupby(["station_id", "market_date"])[f"yes_price_h{hour}"].transform("max").to_numpy()
        mask &= ~(gmax >= consensus)
    return mask, yp, votes, pavg, disp


def enrich(df, idx, hour, yp, votes, pavg, disp, prices, chunk_of, argmax_bucket, unit):
    slug_arr = df["market_slug"].to_numpy()
    close_arr = df["close_ts_unix"].to_numpy()
    bidx = df["bracket_index"].to_numpy()
    bkind = df["bracket_kind"].to_numpy()
    won = df["won_yes"].to_numpy()
    md_arr = df["market_date"].to_numpy()
    stn = df["station_id"].to_numpy()
    ts_arr = df[f"entry_ts_h{hour}"].to_numpy(float)
    ladder_arr = df[f"yes_ask_ladder_h{hour}"].to_numpy()
    # all 24 hourly book snapshots, to price the delayed fill at its nearest book
    book_ts = {h: df[f"book_ts_h{h}"].to_numpy(float) for h in range(24)}
    ask_lad = {h: df[f"yes_ask_ladder_h{h}"].to_numpy() for h in range(24)}

    def nearest_ladder(i, t):
        best_h, best_d = None, None
        for h in range(24):
            bt = book_ts[h][i]
            if not np.isfinite(bt):
                continue
            d = abs(bt - t)
            if best_d is None or d < best_d:
                best_d, best_h = d, h
        return (ask_lad[best_h][i] if best_h is not None else None)

    rows = []
    for i in idx:
        slug = slug_arr[i]
        ser = prices.get(slug)
        e_ts = int(ts_arr[i]); c_ts = int(close_arr[i])
        p_sig = p_60 = mom60 = np.nan
        de_hit = False; de_price = np.nan; de_ts = np.nan
        f10_vwap = np.nan; f10_ok = False; depth_at_de = np.nan
        if ser:
            keys = [t for t, _ in ser]
            p_sig = price_at(ser, keys, e_ts)
            p_60 = price_at(ser, keys, e_ts - 3600)
            if p_sig is not None and p_60 is not None:
                mom60 = p_sig - p_60
            de = delayed_entry(ser, keys, e_ts, c_ts)
            if de is not None:
                de_hit = True; de_ts, de_price = de
                # real executable price: walk the nearest-book ask ladder for $10
                lad = nearest_ladder(i, de_ts)
                depth_at_de = ask_depth_usd(lad, de_price)
                filled, vwap = vwap_fill(lad, 10.0)
                f10_ok = filled >= 9.99
                f10_vwap = vwap if vwap is not None else np.nan
        key = (stn[i], md_arr[i])
        rel = int(bidx[i]) - argmax_bucket.get(key, int(bidx[i]))
        depth3 = ask_depth_usd(ladder_arr[i], 0.03)
        rows.append(dict(
            i=int(i), slug=slug, hour=hour, won=int(won[i]), chunk=chunk_of[md_arr[i]],
            yp=float(yp[i]), pavg=float(pavg[i]), disp=float(disp[i]), votes=int(votes[i]),
            unit=unit[i], bkind=bkind[i], rel=rel,
            hrs_to_res=(c_ts - e_ts) / 3600.0,
            mom60=mom60, de_hit=de_hit, de_price=de_price, de_ts=de_ts,
            depth3=depth3, depth_at_de=depth_at_de, f10_ok=f10_ok, f10_vwap=f10_vwap,
        ))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- walk-forward

def wf_slice(cand, mask_fn, price_col, label):
    """Select the slice on A+B, score on C+D. Reports both halves honestly."""
    ab = cand[cand["chunk"].isin(["A", "B"])]
    cd = cand[cand["chunk"].isin(["C", "D"])]
    ab_m = ab[mask_fn(ab)]
    cd_m = cd[mask_fn(cd)]
    r_ab = slice_row(f"{label} [A+B in-sample]", ab_m["won"].to_numpy(), ab_m[price_col].to_numpy())
    r_cd = slice_row(f"{label} [C+D OUT-OF-SAMPLE]", cd_m["won"].to_numpy(), cd_m[price_col].to_numpy())
    return r_ab, r_cd


# --------------------------------------------------------------------------- main

def main():
    df = pd.read_parquet(PARQUET)
    len(df)
    print("=" * 100)
    print("TAIL SLICE HUNT  --  walk-forward-honest edge search on cheap YES tails")
    print("=" * 100)
    print(f"decision table: {len(df)} rows, {df['market_date'].min()}..{df['market_date'].max()}")

    # chunks
    dates = sorted(set(df["market_date"]))
    split = np.array_split(np.array(dates, dtype=object), 4)
    chunk_of = {d: chr(65 + i) for i, part in enumerate(split) for d in part}
    print("chunks:")
    for i, part in enumerate(split):
        print(f"  {chr(65+i)}: {part[0]} .. {part[-1]} ({len(part)} days)")

    unit = np.array([("F" if "F" in str(l).upper() else ("C" if "C" in str(l).upper() else "?"))
                     for l in df["bracket_label"].to_numpy()])
    # model argmax bucket per station-date (for hot/cold)
    df["p_E"].to_numpy(float)
    argmax_bucket = {}
    for (stn, md), g in df.groupby(["station_id", "market_date"]):
        sub = g[["bracket_index", "p_E"]].dropna()
        if len(sub):
            argmax_bucket[(stn, md)] = int(sub.loc[sub["p_E"].idxmax(), "bracket_index"])

    # ---- reference: exact deployed strict gate (fp<=0.03, consensus 0.40)
    strict_mask, *_ = build_candidates(df, 1, fp_max=0.03, votes_required=4, consensus=0.40)
    won = df["won_yes"].to_numpy()
    ns = int(strict_mask.sum()); ks = int(won[strict_mask].sum())
    print(f"\nEXACT DEPLOYED STRICT GATE (h1, fp<=3c, 4/4 votes, consensus 0.40):")
    print(f"  n={ns}, wins={ks}, wr={ks/ns*100:.2f}%  "
          f"(all Feb-Apr; consensus 0.40 removes 100% of May votes -> chunk D empty)")

    # ---- primary population: widened deployed sleeve (votes>=4, fp<=5c, no consensus)
    mask, yp, votes, pavg, disp = build_candidates(df, 1, fp_max=0.05, votes_required=4)
    idx = np.where(mask)[0]
    slugs = set(df["market_slug"].to_numpy()[idx])
    prices = load_yes_prices(slugs)
    cand = enrich(df, idx, 1, yp, votes, pavg, disp, prices, chunk_of, argmax_bucket, unit)
    print(f"\nPRIMARY POP: votes>=4 @ h1, price 1-5c, vol>=50, ncum>=30, no consensus skip")
    print(f"  n={len(cand)}, wins={int(cand['won'].sum())}, "
          f"wr={cand['won'].mean()*100:.2f}%, delayed-entry(to 2c) hit rate={cand['de_hit'].mean()*100:.1f}%")

    cand["won"].to_numpy()
    cand["yp"].to_numpy()
    # entry-price arrays: signal price, and delayed-2c price (NaN when never dumped)
    cand["de_hit"].to_numpy()
    cand["de_price"].to_numpy()

    be = "  [breakeven wr@2c ~ 2.1%]"
    print("\n" + "=" * 100)
    print("HEADLINE SLICE (c): DELAYED-ENTRY ADVERSE SELECTION  --  the decision-relevant finding")
    print("=" * 100)
    rows = []
    hit = cand[cand["de_hit"]]
    miss = cand[~cand["de_hit"]]
    rows.append(slice_row("DUMPED to <=2c  (rule BUYS these)", hit["won"].to_numpy(), hit["de_price"].to_numpy()))
    rows.append(slice_row("held FIRM >2c   (rule SKIPS these)", miss["won"].to_numpy(), miss["yp"].to_numpy()))
    table("Win rate conditioned on whether YES price dumped to the 2c delayed-entry trigger:", rows, be)
    print("  -> the delayed-entry mechanism structurally buys the dumps and skips the firm-priced winners.")

    print("\n" + "-" * 100)
    print("MOMENTUM (c) at the SIGNAL window: price@h1 vs price 60min earlier (naive 'buy with flow' test)")
    rm = cand.dropna(subset=["mom60"])
    rows = [
        slice_row("RISING into signal (mom>0)", rm[rm["mom60"] > 0]["won"].to_numpy(), rm[rm["mom60"] > 0]["yp"].to_numpy()),
        slice_row("FLAT into signal (mom==0)", rm[rm["mom60"] == 0]["won"].to_numpy(), rm[rm["mom60"] == 0]["yp"].to_numpy()),
        slice_row("FALLING into signal (mom<0)", rm[rm["mom60"] < 0]["won"].to_numpy(), rm[rm["mom60"] < 0]["yp"].to_numpy()),
    ]
    table("(entry price = signal price; in-sample)", rows)

    # ---- remaining slices (in-sample, primary pop, priced at delayed-2c entry where hit else signal)
    def entry_price(sub):
        p = sub["de_price"].to_numpy().copy()
        sig = sub["yp"].to_numpy()
        p = np.where(np.isnan(p), sig, p)
        return p

    print("\n" + "=" * 100)
    print("IN-SAMPLE SLICE TABLES (primary pop; entry price = delayed-2c fill where it dumped, else signal price)")
    print("=" * 100)

    # (a) hot/cold via bracket index vs model argmax bucket
    rows = [
        slice_row("HOT tail  (bracket > argmax)", cand[cand["rel"] > 0]["won"].to_numpy(), entry_price(cand[cand["rel"] > 0])),
        slice_row("AT pred   (bracket == argmax)", cand[cand["rel"] == 0]["won"].to_numpy(), entry_price(cand[cand["rel"] == 0])),
        slice_row("COLD tail (bracket < argmax)", cand[cand["rel"] < 0]["won"].to_numpy(), entry_price(cand[cand["rel"] < 0])),
    ]
    table("(a) HOT vs COLD tail", rows, be)
    rows = [slice_row(f"bracket_kind={k}", cand[cand["bkind"] == k]["won"].to_numpy(), entry_price(cand[cand["bkind"] == k]))
            for k in ("low", "mid", "high")]
    table("(a') bracket_kind", rows)

    # (b) hours-to-resolution
    q = cand["hrs_to_res"]
    rows = []
    for lab, lo, hi in [("<=12h", -1, 12), ("12-18h", 12, 18), ("18-24h", 18, 24), (">24h", 24, 1e9)]:
        s = cand[(q > lo) & (q <= hi)]
        rows.append(slice_row(lab, s["won"].to_numpy(), entry_price(s)))
    table("(b) hours-to-resolution", rows, be)

    # (d) ask depth at 3c
    rows = []
    med = cand["depth3"].median()
    for lab, mm in [(f"THIN ask (<=${med:.0f} @<=3c)", cand["depth3"] <= med),
                    (f"THICK ask (>${med:.0f} @<=3c)", cand["depth3"] > med)]:
        s = cand[mm]
        rows.append(slice_row(lab, s["won"].to_numpy(), entry_price(s)))
    table("(d) ask-depth at entry (median split)", rows, be)

    # (e) model claim level
    rows = []
    for lab, lo, hi in [("claim 2-5%", 0.02, 0.05), ("claim 5-10%", 0.05, 0.10),
                        ("claim 10-15%", 0.10, 0.15), ("claim >15%", 0.15, 1.0)]:
        s = cand[(cand["pavg"] > lo) & (cand["pavg"] <= hi)]
        rows.append(slice_row(lab, s["won"].to_numpy(), entry_price(s)))
    table("(e) model claim level (avg of 4 signals)", rows, be)

    # (f) vote dispersion
    rows = []
    dmed = cand["disp"].median()
    for lab, mm in [("TIGHT votes (disp<=med)", cand["disp"] <= dmed),
                    ("SPREAD votes (disp>med)", cand["disp"] > dmed)]:
        s = cand[mm]
        rows.append(slice_row(lab, s["won"].to_numpy(), entry_price(s)))
    table("(f) vote dispersion across 4 signals (median split)", rows, be)

    # (g) unit
    rows = [slice_row(f"unit {u}", cand[cand["unit"] == u]["won"].to_numpy(), entry_price(cand[cand["unit"] == u]))
            for u in ("C", "F")]
    table("(g) station unit", rows, be)

    # (b') entry hour 0-6 (rebuild across hours)
    print("\n(b'') entry-hour sweep (votes>=4 @ each local hour 0-6, price 1-5c; in-sample)")
    for h in range(7):
        m_h, yph, vh, pah, dh = build_candidates(df, h, fp_max=0.05, votes_required=4)
        wn = won[m_h]; pp = yph[m_h]
        r = slice_row(f"hour {h}", wn, pp)
        print(fmt_row(r))

    # ---- WALK-FORWARD on the whole primary sleeve + top in-sample slices
    print("\n" + "=" * 100)
    print("WALK-FORWARD (select on A+B, score on C+D)")
    print("=" * 100)
    cand_ep = cand.copy()
    cand_ep["ep"] = entry_price(cand)

    def report_wf(label, mask_fn):
        r_ab, r_cd = wf_slice(cand_ep, mask_fn, "ep", label)
        print(fmt_row(r_ab, be))
        print(fmt_row(r_cd, be))
        print()

    report_wf("WHOLE votes>=4 sleeve", lambda d: np.ones(len(d), bool))
    report_wf("HOT tails (rel>0)", lambda d: d["rel"].to_numpy() > 0)
    report_wf("claim>15%", lambda d: d["pavg"].to_numpy() > 0.15)
    report_wf("unit F", lambda d: d["unit"].to_numpy() == "F")
    report_wf("held-firm flip (de_hit False)", lambda d: ~d["de_hit"].to_numpy())
    report_wf("TIGHT votes (disp<=median)", lambda d: d["disp"].to_numpy() <= cand["disp"].median())

    # ---- FILL REALISM: does $10 actually fill at the 2c PMD print, or do you pay the ask?
    print("=" * 100)
    print("FILL REALISM  --  PMD 1-min print vs executable L2 ask ($10 order)")
    print("=" * 100)
    ent = cand[cand["de_hit"]].copy()
    got10 = ent[ent["depth_at_de"] >= 9.99]
    print(f"  delayed-2c entries: {len(ent)}   (mean PMD fill price {ent['de_price'].mean()*100:.2f}c)")
    print(f"  rows with >=$10 of YES ask AT/BELOW the PMD print price: "
          f"{len(got10)}/{len(ent)} ({len(got10)/len(ent)*100:.1f}%)")
    fillable = ent[ent["f10_ok"] & ent["f10_vwap"].notna()].copy()
    print(f"  rows where $10 fills at all (walking the ask, any price): {len(fillable)}/{len(ent)}")
    print(f"  mean realized VWAP for a real $10 ask-walk: {fillable['f10_vwap'].mean()*100:.2f}c "
          f"(vs PMD-print {fillable['de_price'].mean()*100:.2f}c)")
    print("\n  EV recomputed at the REAL $10 ask-walk VWAP (this is the executable sleeve):")
    for lab, sub in [("WHOLE sleeve", fillable),
                     ("  A+B in-sample", fillable[fillable["chunk"].isin(["A", "B"])]),
                     ("  C+D OUT-OF-SAMPLE", fillable[fillable["chunk"].isin(["C", "D"])]),
                     ("  unit F (best near-miss)", fillable[fillable["unit"] == "F"]),
                     ("  unit F C+D OOS", fillable[(fillable["unit"] == "F") & fillable["chunk"].isin(["C", "D"])])]:
        r = slice_row(lab, sub["won"].to_numpy(), sub["f10_vwap"].to_numpy())
        print(fmt_row(r))
    print("\n  -> the 2c print is a mid/last-trade; there is essentially no $10 of ask there.")
    print("     Real fills land near ~4c, breakeven jumps to ~4.4%, realized wr ~3% => NEGATIVE EV.")

    # ---- exit variants on the whole sleeve (hold vs TP), delayed-2c entries only
    print("\n" + "=" * 100)
    print("EXIT VARIANTS (delayed-2c entries only; PMD-print entry, optimistic; TP needs a real print)")
    print("=" * 100)
    entered = cand[cand["de_hit"]].copy()
    slug_arr = df["market_slug"].to_numpy(); close_arr = df["close_ts_unix"].to_numpy()
    for tp in (None, 0.10, 0.20):
        pnl = 0.0; n_tp = 0
        wins_wt = 0
        for _, row in entered.iterrows():
            p_in = row["de_price"]; i = row["i"]
            ser = prices.get(slug_arr[i]);
            if not ser:
                continue
            keys = [t for t, _ in ser]
            c_ts = int(close_arr[i])
            if tp is not None:
                hit = cross_up(ser, keys, int(row["de_ts"]), c_ts, p_in + tp)
                if hit is not None:
                    exit_p = p_in + tp
                    net = 10.0 * ((exit_p - p_in) / p_in - THETA * (1 - p_in))
                    pnl += net; n_tp += 1; wins_wt += 1
                    continue
            # held to resolution
            win = row["won"] > 0.5
            net = 10.0 * (((1 - p_in) / p_in - THETA * (1 - p_in)) if win else (-1 - THETA * (1 - p_in)))
            pnl += net
            if win:
                wins_wt += 1
        lab = "hold-to-resolution" if tp is None else f"TP +{int(tp*100)}c then hold"
        print(f"  {lab:<24} entered={len(entered)}  TP-hits={n_tp}  gross wins={wins_wt}  PnL@$10=${pnl:+.2f}")

    print("\nDone.")


if __name__ == "__main__":
    main()
