"""Feasibility backtest: METAR "observation sniping" for Polymarket daily-high markets.

Quantifies the edge from buying NO on a provably-dead bracket (running observed
max already above the bracket's upper bound) or YES on the post-peak leading
bracket, when the thin 10-min market has not yet repriced.

READ-ONLY on src/ and backtest/lib. Data sources:
  - backtest/data/polymarket_history.db  (markets, prices[10-min mid], metrics)
  - backtest/data/decision_table_may11plus_l2.parquet (bracket bounds, actual, won_yes)
  - <scratchpad>/metar_{ICAO}.csv  (IEM ASOS tmpf, UTC-stamped, degF)

Prints a dense report to stdout; writes event-level CSVs to the scratchpad.
No server access. No writes outside scratchpad.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np
import pandas as pd
import pytz

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(REPO, "backtest", "data", "polymarket_history.db")
DT = os.path.join(REPO, "backtest", "data", "decision_table_may11plus_l2.parquet")
SCRATCH = (
    r"C:\Users\leowq\AppData\Local\Temp\claude"
    r"\c--Users-leowq-OneDrive-Desktop-hightempbot"
    r"\a9ed1d4f-dff5-472f-9a13-e9a899375ce4\scratchpad"
)

TZ = {
    "KLAX": "America/Los_Angeles",
    "KDAL": "America/Chicago",
    "KHOU": "America/Chicago",
    "KMIA": "America/New_York",
    "KORD": "America/Chicago",
}
SNAP = 600  # price cadence, seconds


def c2f(c):
    return c * 9.0 / 5.0 + 32.0


def load_metar(icao):
    m = pd.read_csv(os.path.join(SCRATCH, f"metar_{icao}.csv"))
    m = m[m.tmpf != "M"].copy()
    m["tmpf"] = pd.to_numeric(m.tmpf, errors="coerce")
    m = m.dropna(subset=["tmpf"])
    m["valid"] = pd.to_datetime(m.valid, utc=True)
    m["ts"] = (
        (m.valid - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds().astype("int64")
    )
    return m[["ts", "tmpf"]].sort_values("ts").reset_index(drop=True)


def main():
    con = sqlite3.connect(DB)
    dt = pd.read_parquet(DT)
    dt = dt[dt.station_id.isin(TZ)].copy()

    dead_events = []   # one row per (station-day, bracket) that becomes provably dead
    lead_events = []   # one row per (station-day) leading-bracket snipe at fixed local hours
    day_rows = []      # per station-day diagnostics

    for icao, tzn in TZ.items():
        tz = pytz.timezone(tzn)
        metar = load_metar(icao)
        st = dt[dt.station_id == icao]
        mdates = sorted(d for d in st.market_date.unique() if d <= "2026-05-19")
        for md in mdates:
            y, mo, d = map(int, md.split("-"))
            local_mid = tz.localize(pd.Timestamp(y, mo, d)).timestamp()
            close_ts = int(pd.Timestamp(y, mo, d, tz="UTC").timestamp()) + 86400  # 00:00 UTC md+1
            # METAR over the resolution local day, up to close (tradeable window)
            day_obs = metar[(metar.ts >= local_mid) & (metar.ts < close_ts)]
            if len(day_obs) < 5:
                continue
            obs_ts = day_obs.ts.to_numpy()
            obs_f = day_obs.tmpf.to_numpy()
            run_max = np.maximum.accumulate(obs_f)  # running max degF

            brk = st[st.market_date == md].copy()
            if brk.empty:
                continue
            wu_f = c2f(brk.actual_high_c.iloc[0])

            # afternoon price-coverage gate: need dense snaps after local noon
            slugs = tuple(brk.market_slug.tolist())
            ph = "?" if len(slugs) == 1 else ",".join("?" * len(slugs))
            px = pd.read_sql(
                f"SELECT market_slug, ts_unix, price yes FROM prices "
                f"WHERE side='Yes' AND market_slug IN ({ph}) "
                f"AND ts_unix>=? AND ts_unix<=?",
                con, params=[*slugs, int(local_mid), int(close_ts)],
            )
            if px.empty:
                continue
            noon = local_mid + 12 * 3600
            n_pm = px[px.ts_unix >= noon].ts_unix.nunique()
            if n_pm < 18:
                continue

            mx = pd.read_sql(
                f"SELECT market_slug, ts_unix, spread, liquidity FROM metrics "
                f"WHERE market_slug IN ({ph}) AND ts_unix>=? AND ts_unix<=?",
                con, params=[*slugs, int(local_mid), int(close_ts)],
            )

            final_run_max = run_max[-1]
            day_rows.append(dict(
                icao=icao, md=md, wu_f=wu_f, metar_close_max=final_run_max,
                metar_eq_wu=int(round(final_run_max) == round(wu_f)),
                n_pm=n_pm, n_obs=len(day_obs),
            ))

            for _, b in brk.iterrows():
                slug = b.market_slug
                p = px[px.market_slug == slug].sort_values("ts_unix")
                if p.empty:
                    continue
                pr_ts = p.ts_unix.to_numpy()
                pr_yes = p.yes.to_numpy()
                mm = mx[mx.market_slug == slug].sort_values("ts_unix")

                def px_at(t):
                    """Yes price at first snapshot >= t (or None)."""
                    i = np.searchsorted(pr_ts, t, side="left")
                    if i >= len(pr_ts):
                        return None, None
                    return float(pr_yes[i]), int(pr_ts[i])

                def px_before(t):
                    i = np.searchsorted(pr_ts, t, side="left") - 1
                    if i < 0:
                        return None
                    return float(pr_yes[i])

                def spread_at(t):
                    if mm.empty:
                        return np.nan, np.nan
                    j = np.searchsorted(mm.ts_unix.to_numpy(), t, side="left")
                    j = min(j, len(mm) - 1)
                    return float(mm.spread.iloc[j]), float(mm.liquidity.iloc[j])

                hi_f = c2f(b.hi_c) if np.isfinite(b.hi_c) else np.inf

                # ---- DEAD-bracket event: running max first exceeds hi_f ----
                if np.isfinite(hi_f):
                    dead_idx = np.argmax(run_max > hi_f) if (run_max > hi_f).any() else -1
                    if dead_idx >= 0:
                        t_dead = int(obs_ts[dead_idx])
                        # only interesting if the bracket had a live market pre-dead
                        pre = pr_yes[pr_ts < t_dead]
                        was_live = pre.size > 0 and pre.max() >= 0.05
                        p_at, ts_at = px_at(t_dead)
                        p_bef = px_before(t_dead)
                        if p_at is not None:
                            # snapshots until yes<=0.05 after t_dead
                            after = [(tt, vv) for tt, vv in zip(pr_ts, pr_yes) if tt >= t_dead]
                            n_to_05 = None
                            for k, (tt, vv) in enumerate(after):
                                if vv <= 0.05:
                                    n_to_05 = k
                                    break
                            sp, liq = spread_at(t_dead)
                            dead_events.append(dict(
                                icao=icao, md=md, slug=slug, kind=b.bracket_kind,
                                label=b.bracket_label, hi_f=hi_f, wu_f=wu_f,
                                won_yes=int(b.won_yes), was_live=int(was_live),
                                t_dead=t_dead, yes_before=p_bef, yes_at=p_at,
                                yes_min_after=float(min(v for _, v in after)),
                                snaps_to_05=n_to_05, spread=sp, liquidity=liq,
                                metar_eq_wu=int(round(final_run_max) == round(wu_f)),
                            ))

            # ---- LEADING-bracket YES snipe at fixed local hours ----
            for hh in (15, 16, 17):
                t_h = local_mid + hh * 3600
                if t_h >= close_ts:
                    continue
                # running max as of t_h
                idx = np.searchsorted(obs_ts, t_h, side="right") - 1
                if idx < 0:
                    continue
                rm = run_max[idx]
                rm_c = (rm - 32) * 5 / 9
                lead = brk[(brk.lo_c < rm_c) & (rm_c <= brk.hi_c)]
                if lead.empty:
                    continue
                lb = lead.iloc[0]
                p = px[px.market_slug == lb.market_slug].sort_values("ts_unix")
                if p.empty:
                    continue
                i = np.searchsorted(p.ts_unix.to_numpy(), t_h, side="left")
                if i >= len(p):
                    continue
                yes_h = float(p.yes.iloc[i])
                # obs stability: has running max been flat for >=60min?
                idx60 = np.searchsorted(obs_ts, t_h - 3600, side="right") - 1
                flat = idx60 >= 0 and run_max[idx60] == rm
                lead_events.append(dict(
                    icao=icao, md=md, hour=hh, slug=lb.market_slug,
                    label=lb.bracket_label, yes_h=yes_h, won_yes=int(lb.won_yes),
                    flat60=int(flat), rm_f=rm, wu_f=wu_f,
                ))

    de = pd.DataFrame(dead_events)
    le = pd.DataFrame(lead_events)
    dd = pd.DataFrame(day_rows)
    de.to_csv(os.path.join(SCRATCH, "events_dead.csv"), index=False)
    le.to_csv(os.path.join(SCRATCH, "events_lead.csv"), index=False)
    dd.to_csv(os.path.join(SCRATCH, "events_days.csv"), index=False)
    report(de, le, dd)


def report(de, le, dd):
    L = print
    L("=" * 78)
    L("METAR OBSERVATION-SNIPING FEASIBILITY  (5 US stations, Feb-May 2026)")
    L("=" * 78)
    L(f"Station-days analyzed (dense pm coverage): {len(dd)}")
    L(f"  METAR close-max rounds to WU actual: {dd.metar_eq_wu.mean()*100:.1f}%  "
      f"(mismatch = the tail risk when acting on obs)")
    L("")

    # ---------- DEAD BRACKET ----------
    L("-" * 78)
    L("PART A -- DEAD-BRACKET NO SNIPE (running max already above bracket high)")
    L("-" * 78)
    live = de[de.was_live == 1].copy()   # bracket had a real market before dying
    L(f"Dead-bracket events total: {len(de)};  of which had a LIVE market pre-death: {len(live)}")
    # correctness of the dead call
    wrong = de[de.won_yes == 1]
    L(f"'Dead' calls where the bracket actually WON (METAR/WU mismatch): "
      f"{len(wrong)}/{len(de)} = {len(wrong)/max(len(de),1)*100:.2f}%")
    L("")
    # opportunity: at the obs that confirms death, is YES still buyable?
    for thr in (0.05, 0.10, 0.20):
        opp = live[live.yes_at > thr]
        L(f"  LIVE dead-brackets still priced YES>{thr:.2f} at first snap after obs: "
          f"{len(opp)}  ({len(opp)/max(len(live),1)*100:.1f}% of live)")
    L("")
    L("  Repricing speed (LIVE dead-brackets, YES>0.05 at obs): "
      "10-min snaps until YES<=0.05")
    opp = live[live.yes_at > 0.05].copy()
    if len(opp):
        s = opp.snaps_to_05.dropna()
        L(f"    n={len(opp)}  already<=0.05 at obs-snap(0): {(opp.snaps_to_05==0).mean()*100:.0f}%")
        for q in (0.25, 0.5, 0.75, 0.9):
            L(f"    snaps_to_0.05 p{int(q*100)} = {s.quantile(q):.0f}  (~{s.quantile(q)*10:.0f} min)")
        L(f"    never repriced by close: {opp.snaps_to_05.isna().mean()*100:.0f}%")
        # did market front-run the (hourly) obs?
        fr = opp[(opp.yes_before.notna()) & (opp.yes_before <= 0.05)]
        L(f"    yes already<=0.05 the snap BEFORE our hourly obs (front-run/late-detect): "
          f"{len(fr)}/{len(opp)} = {len(fr)/len(opp)*100:.0f}%")
        # realizable edge net of half-spread
        opp = opp.copy()
        opp["edge_mid"] = opp.yes_at            # value recovered per NO share = yes_price
        opp["edge_net"] = opp.yes_at - opp.spread.fillna(0) / 2
        L(f"    edge/share at obs-snap: mid median={opp.edge_mid.median():.3f} "
          f"mean={opp.edge_mid.mean():.3f}; net-of-halfspread median={opp.edge_net.median():.3f} "
          f"mean={opp.edge_net.mean():.3f}")
        L(f"    liquidity($) at event: median={opp.liquidity.median():.0f} "
          f"p90={opp.liquidity.quantile(.9):.0f}")
    L("")

    # ---------- LEADING BRACKET ----------
    L("-" * 78)
    L("PART B -- LEADING-BRACKET YES SNIPE (buy the bracket holding the running max)")
    L("-" * 78)
    for hh in (15, 16, 17):
        sub = le[le.hour == hh]
        if sub.empty:
            continue
        L(f"  Local {hh}:00  n_days={len(sub)}  "
          f"leading-bracket won_yes={sub.won_yes.mean()*100:.0f}%  "
          f"(flat-60min subset won={sub[sub.flat60==1].won_yes.mean()*100:.0f}% "
          f"n={sub.flat60.sum()})")
        for cap in (0.75, 0.90):
            buy = sub[(sub.yes_h <= cap) & (sub.flat60 == 1)]
            if len(buy):
                ev = (buy.won_yes - buy.yes_h).mean()
                L(f"      buy YES<= {cap:.2f} & flat60: n={len(buy)} "
                  f"win={buy.won_yes.mean()*100:.0f}% avg_entry={buy.yes_h.mean():.3f} "
                  f"EV/$={ev:+.3f} total_pnl/$1each={ (buy.won_yes-buy.yes_h).sum():+.2f}")
    L("")

    # ---------- HYPOTHETICAL PNL ----------
    L("-" * 78)
    L("PART C -- HYPOTHETICAL PnL, honest (mid-price entry, half-spread haircut)")
    L("-" * 78)
    # Rule NO: at first snap after obs confirms a LIVE bracket dead, buy NO if
    # yes_at in [0.05, 0.60] (avoid already-dead <0.05 and never-touch huge edges
    # that are likely stale/untradeable). Hold to resolution.
    r = live[(live.yes_at >= 0.05) & (live.yes_at <= 0.60)].copy()
    r["no_price"] = 1 - r.yes_at + r.spread.fillna(0) / 2     # pay NO ask ~ mid+halfspread
    r = r[r.no_price < 1.0]
    r["pnl_per_share"] = np.where(r.won_yes == 0, 1 - r.no_price, -r.no_price)
    r["ret"] = r.pnl_per_share / r.no_price
    L(f"  DEAD-NO rule: n_trades={len(r)}  win={ (r.won_yes==0).mean()*100:.1f}%")
    L(f"    avg NO entry(net)={r.no_price.mean():.3f}  EV/$stake={r.ret.mean():+.3f}  "
      f"median ret={r.ret.median():+.3f}")
    L(f"    per-$1000 stake/trade total PnL over sample = "
      f"${(r.ret*1000).sum():,.0f} across {len(r)} trades "
      f"({r.icao.nunique()} stns, {r.md.nunique()} days)")
    # cap fill at min(stake, 5% of liquidity) to be realistic
    stake_cap = np.minimum(750, 0.05 * r.liquidity.fillna(0))
    r["pnl_capped"] = r.ret * stake_cap
    L(f"    with fill=min($750, 5% liquidity): total PnL=${r.pnl_capped.sum():,.0f} "
      f"(avg fill ${stake_cap.mean():.0f})")
    L("")
    L(f"  Events/day/station (DEAD-NO, live, yes 0.05-0.60): "
      f"{len(r)/max(dd.shape[0],1):.2f} per station-day")
    L("=" * 78)


if __name__ == "__main__":
    main()
