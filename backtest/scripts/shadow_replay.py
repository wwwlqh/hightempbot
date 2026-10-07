"""Shadow-replay parity: live ledger bets vs backtest per-bet expectation.

Phase 2 (2026-07-16). Joins resolved LIVE bets (from a ledger sqlite) against
the backtest's persisted per-bet stream (produced by
`champion_honest_report.py` / `measure_tp_sl.py` via honest_report.persist_bets)
on (station, target_date, bracket bounds, side), and reports:

  - matched / live-only / backtest-only counts
  - fill-price deltas (live executed vs backtest VWAP)
  - outcome agreement (did both call the same win/loss?)
  - PnL comparison
  - claimed-vs-realized reliability for BOTH streams

It degrades gracefully: if either side has no data for the window (today's
state, because the PMD price ingest stopped 2026-05-20 and no fresh backtest
per-bet parquet can be built past then), it says so and reports whatever it can
instead of crashing.

--------------------------------------------------------------------------------
INTENDED NIGHTLY USAGE
--------------------------------------------------------------------------------
Once the PMD ingest + decision-table rebuild are current again (see
backtest/RUNBOOK_parity.md), run nightly after the rebuild:

    # 1. refresh the backtest per-bet expectation over the recent window
    python backtest/scripts/champion_honest_report.py

    # 2. compare the last 30d of resolved live bets to that expectation
    python backtest/scripts/shadow_replay.py \
        --ledger ~/hightempbot/data/hightempbot.db \
        --bets   backtest/results/bets/ \
        --start  2026-05-01 --end 2026-05-31

Alert if: |mean fill delta| grows, outcome-agreement drops, or the live
overconfidence gap diverges from the backtest's (calibration drift).
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backtest.lib import honest_report as hr  # noqa: E402


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def _to_celsius(low, high, unit):
    """Convert native-unit bracket bounds to Celsius (mirrors sweep_lib parser)."""
    if low is None or high is None:
        return None, None
    lo, hi = float(low), float(high)
    if str(unit).upper() == "F":
        lo = _f_to_c(lo) if math.isfinite(lo) else lo
        hi = _f_to_c(hi) if math.isfinite(hi) else hi
    return lo, hi


def _bound_key(val, tol: float) -> str:
    """Quantize a Celsius bound to a tolerance grid; keep +/-inf as sentinels."""
    if val is None:
        return "na"
    v = float(val)
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    if math.isnan(v):
        return "na"
    return f"{round(v / tol) * tol:.2f}"


def _norm_side(side: str) -> str:
    s = str(side).strip().upper()
    return "NO" if s == "NO" else ("YES" if s in ("YES", "Y") else s)


def join_key(station, target_date, side, lo_c, hi_c, tol) -> tuple:
    return (str(station), str(target_date)[:10], _norm_side(side),
            _bound_key(lo_c, tol), _bound_key(hi_c, tol))


# --------------------------------------------------------------------------- loaders

def load_live_bets(ledger_path: Path, start: str | None, end: str | None) -> pd.DataFrame:
    """Resolved, non-dry-run live bets from the ledger, one row per ledger entry.

    Excludes event_type='dry_run' and outcome='CANCELLED' per the parity spec.
    """
    conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ledger)")}
    except sqlite3.OperationalError:
        conn.close()
        raise SystemExit(f"[shadow_replay] no `ledger` table in {ledger_path}")
    have = lambda c: c if c in cols else "NULL"
    q = (
        f"SELECT {have('bet_ts')} AS bet_ts, {have('station_id')} AS station_id, "
        f"{have('target_date')} AS target_date, {have('side')} AS side, "
        f"{have('p_model')} AS p_model, {have('prob_safe_floor')} AS prob_safe_floor, "
        f"{have('fill_price')} AS fill_price, {have('fill_size')} AS fill_size, "
        f"{have('outcome')} AS outcome, {have('pnl')} AS pnl, "
        f"{have('event_type')} AS event_type, {have('event_detail')} AS event_detail "
        "FROM ledger "
        "WHERE COALESCE(event_type,'') != 'dry_run' AND COALESCE(outcome,'') != 'CANCELLED'"
    )
    rows = list(conn.execute(q))
    conn.close()
    out = []
    for (bet_ts, station_id, target_date, side, p_model, prob_safe_floor,
         fill_price, fill_size, outcome, pnl, event_type, event_detail) in rows:
        td = str(target_date)[:10] if target_date else None
        if start and td and td < start:
            continue
        if end and td and td > end:
            continue
        detail = {}
        if event_detail:
            try:
                detail = json.loads(event_detail)
            except (json.JSONDecodeError, TypeError):
                detail = {}
        unit = detail.get("bracket_unit")
        lo_c, hi_c = _to_celsius(detail.get("bracket_low"), detail.get("bracket_high"), unit)
        s = _norm_side(side)
        # Live claimed P(this side wins): NO -> 1-p_model (== prob_safe_floor), YES -> p_model.
        claimed = prob_safe_floor if s == "NO" else p_model
        oc = str(outcome).upper() if outcome else ""
        won = True if oc == "WIN" else (False if oc == "LOSS" else None)
        out.append({
            "bet_ts": bet_ts, "station_id": station_id, "target_date": td,
            "side": s, "strategy": detail.get("strategy"),
            "bracket_unit": unit, "lo_c": lo_c, "hi_c": hi_c,
            "claimed_p": float(claimed) if claimed is not None else None,
            "fill_price": float(fill_price) if fill_price is not None else None,
            "fill_size": float(fill_size) if fill_size is not None else None,
            "outcome": oc, "won": won,
            "pnl": float(pnl) if pnl is not None else 0.0,
        })
    return pd.DataFrame(out)


def load_backtest_bets(bets_path: Path, start: str | None, end: str | None) -> pd.DataFrame:
    """Backtest per-bet records from a parquet file or a directory of them."""
    if bets_path.is_dir():
        files = sorted(bets_path.glob("*.parquet"))
    elif bets_path.exists():
        files = [bets_path]
    else:
        files = []
    if not files:
        return pd.DataFrame()
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f).assign(_src=f.name))
        except Exception as exc:  # noqa: BLE001
            print(f"[shadow_replay] warn: could not read {f}: {exc}", file=sys.stderr)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "market_date" in df.columns:
        md = df["market_date"].astype(str).str.slice(0, 10)
        mask = pd.Series(True, index=df.index)
        if start:
            mask &= (md >= start)
        if end:
            mask &= (md <= end)
        df = df[mask]
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- aggregation

def _agg_live(live: pd.DataFrame, tol: float) -> dict:
    """Aggregate live rows per join key (idempotent top-ups collapse to one slot)."""
    agg: dict[tuple, dict] = {}
    for r in live.itertuples():
        key = join_key(r.station_id, r.target_date, r.side, r.lo_c, r.hi_c, tol)
        a = agg.setdefault(key, {
            "n_fills": 0, "notional": 0.0, "fill_px_num": 0.0, "size": 0.0,
            "pnl": 0.0, "won": None, "claimed_p": r.claimed_p, "strategy": r.strategy,
            "station_id": r.station_id, "target_date": r.target_date, "side": r.side,
        })
        a["n_fills"] += 1
        a["pnl"] += r.pnl or 0.0
        if r.fill_price is not None and r.fill_size:
            a["notional"] += r.fill_price * r.fill_size
            a["size"] += r.fill_size
        if r.won is not None:
            a["won"] = r.won  # all fills on one market/side resolve identically
    for a in agg.values():
        a["fill_price"] = (a["notional"] / a["size"]) if a["size"] > 0 else None
    return agg


def _agg_backtest(bt: pd.DataFrame, tol: float) -> dict:
    agg: dict[tuple, dict] = {}
    for r in bt.itertuples():
        lo_c = getattr(r, "lo_c", None)
        hi_c = getattr(r, "hi_c", None)
        key = join_key(r.station_id, r.market_date, r.side, lo_c, hi_c, tol)
        # If the same key appears in multiple periods (it shouldn't within one
        # window), keep the first and sum stakes/pnl defensively.
        a = agg.setdefault(key, {
            "stake": 0.0, "pnl": 0.0, "fill_vwap": getattr(r, "fill_vwap", None),
            "won": bool(getattr(r, "won")), "claimed_p": float(getattr(r, "claimed_p")),
            "strategy": getattr(r, "strategy", None),
            "station_id": r.station_id, "target_date": str(r.market_date)[:10], "side": r.side,
        })
        a["stake"] += float(getattr(r, "stake", 0.0) or 0.0)
        a["pnl"] += float(getattr(r, "pnl", 0.0) or 0.0)
    return agg


# --------------------------------------------------------------------------- report

def _reliability_block(name: str, records: list[dict]) -> list[str]:
    m = hr.slice_metrics(records)
    if m["n"] == 0:
        return [f"  {name}: no resolved bets"]
    lines = [
        f"  {name}: n={m['n']}  win={m['win_rate']*100:.1f}%+/-{m['win_rate_se']*100:.1f}  "
        f"claim={m['claimed_mean']*100:.1f}%  real={m['realized_mean']*100:.1f}%  "
        f"overconf={m['overconfidence_pp']:+.1f}pp  ROS={m['ros_pct']:+.2f}%  pnl=${m['pnl']:+.2f}"
    ]
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", type=Path, required=True, help="path to a ledger sqlite db")
    ap.add_argument("--bets", type=Path,
                    default=REPO_ROOT / "backtest" / "results" / "bets",
                    help="backtest per-bet parquet file OR directory (default: backtest/results/bets)")
    ap.add_argument("--start", type=str, default=None, help="window start YYYY-MM-DD (target_date)")
    ap.add_argument("--end", type=str, default=None, help="window end YYYY-MM-DD (target_date)")
    ap.add_argument("--tol-c", type=float, default=0.2, help="bracket-bound match tolerance (Celsius)")
    ap.add_argument("--json", action="store_true", help="also emit a machine-readable JSON footer")
    args = ap.parse_args()

    live = load_live_bets(args.ledger, args.start, args.end)
    bt = load_backtest_bets(args.bets, args.start, args.end)

    window = f"{args.start or '(open)'} .. {args.end or '(open)'}"
    print("=" * 92)
    print("SHADOW REPLAY — live ledger vs backtest per-bet expectation".replace("—", "-"))
    print(f"  ledger : {args.ledger}")
    print(f"  bets   : {args.bets}")
    print(f"  window : {window}   (bracket tol +/-{args.tol_c}C)")
    print("=" * 92)

    live_n = 0 if live.empty else len(live)
    bt_n = 0 if bt.empty else len(bt)
    print(f"Loaded: {live_n} live ledger bets (resolved+pending, non-dry-run, non-cancelled), "
          f"{bt_n} backtest per-bet records")

    # Graceful degradation ----------------------------------------------------
    if live.empty and bt.empty:
        print("\nNO DATA on EITHER side for this window. Nothing to compare.")
        print("This is the expected state until the PMD ingest + decision-table rebuild")
        print("are current again (see backtest/RUNBOOK_parity.md).")
        if args.json:
            print(json.dumps({"status": "empty_both", "matched": 0}))
        return

    live_agg = _agg_live(live, args.tol_c) if not live.empty else {}
    bt_agg = _agg_backtest(bt, args.tol_c) if not bt.empty else {}

    if not live_agg:
        print("\nLIVE side is EMPTY for this window (no resolved/placed live bets).")
    if not bt_agg:
        print("\nBACKTEST side is EMPTY for this window (no per-bet parquet covering it).")
        print("Rebuild the decision table + re-run champion_honest_report.py once the")
        print("PMD price ingest is current again (see backtest/RUNBOOK_parity.md).")

    live_keys = set(live_agg)
    bt_keys = set(bt_agg)
    matched = sorted(live_keys & bt_keys)
    live_only = sorted(live_keys - bt_keys)
    bt_only = sorted(bt_keys - live_keys)

    print(f"\nJoin on (station, target_date, side, bracket bounds):")
    print(f"  matched         : {len(matched)}")
    print(f"  live-only        : {len(live_only)}")
    print(f"  backtest-only    : {len(bt_only)}")

    # Matched detail ----------------------------------------------------------
    fill_deltas = []
    outcome_agree = 0
    outcome_known = 0
    live_pnl_m = 0.0
    bt_pnl_m = 0.0
    matched_rows = []
    for k in matched:
        lv = live_agg[k]
        bv = bt_agg[k]
        live_pnl_m += lv["pnl"]
        bt_pnl_m += bv["pnl"]
        if lv["fill_price"] is not None and bv["fill_vwap"] is not None:
            fill_deltas.append(lv["fill_price"] - bv["fill_vwap"])
        if lv["won"] is not None:
            outcome_known += 1
            if bool(lv["won"]) == bool(bv["won"]):
                outcome_agree += 1
        matched_rows.append((k, lv, bv))

    if matched:
        print("\nMatched-bet parity:")
        if fill_deltas:
            md = pd.Series(fill_deltas)
            print(f"  fill delta (live - backtest): mean={md.mean():+.4f}  "
                  f"median={md.median():+.4f}  |max|={md.abs().max():.4f}  (n={len(md)})")
        else:
            print("  fill delta: n/a (live fills missing)")
        if outcome_known:
            print(f"  outcome agreement: {outcome_agree}/{outcome_known} "
                  f"({outcome_agree/outcome_known*100:.0f}%)")
        else:
            print("  outcome agreement: n/a (no resolved live outcomes matched)")
        print(f"  PnL over matched: live=${live_pnl_m:+.2f}  backtest=${bt_pnl_m:+.2f}")
        print("  sample (up to 8):")
        print(f"    {'station':<7} {'date':<10} {'side':<4} {'strat':<5} "
              f"{'live_fill':>9} {'bt_fill':>8} {'live_won':>8} {'bt_won':>7}")
        for k, lv, bv in matched_rows[:8]:
            lf = f"{lv['fill_price']:.3f}" if lv["fill_price"] is not None else "  -  "
            bf = f"{bv['fill_vwap']:.3f}" if bv["fill_vwap"] is not None else "  -  "
            print(f"    {str(lv['station_id']):<7} {str(lv['target_date']):<10} "
                  f"{lv['side']:<4} {str(lv.get('strategy') or bv.get('strategy') or ''):<5} "
                  f"{lf:>9} {bf:>8} {str(lv['won']):>8} {str(bv['won']):>7}")

    # Reliability for both streams -------------------------------------------
    live_recs = [
        {"claimed_p": a["claimed_p"], "won": a["won"], "stake": (a["size"] or 0.0),
         "pnl": a["pnl"], "entry_ts": 0}
        for a in live_agg.values() if a["claimed_p"] is not None and a["won"] is not None
    ]
    bt_recs = [
        {"claimed_p": a["claimed_p"], "won": a["won"], "stake": a["stake"],
         "pnl": a["pnl"], "entry_ts": 0}
        for a in bt_agg.values()
    ]
    print("\nCalibration (claimed P(win) vs realized), per stream:")
    for line in _reliability_block("LIVE    ", live_recs):
        print(line)
    for line in _reliability_block("BACKTEST", bt_recs):
        print(line)

    if live_only:
        print(f"\nLive-only sample (up to 6) — placed live but not in backtest expectation:".replace("—", "-"))
        for k in live_only[:6]:
            a = live_agg[k]
            print(f"  {a['station_id']:<7} {a['target_date']}  {a['side']:<3} "
                  f"strat={a.get('strategy')}  claimed={a['claimed_p']}  won={a['won']}  pnl=${a['pnl']:+.2f}")
    if bt_only:
        print(f"\nBacktest-only sample (up to 6) - expected by backtest but no live bet:")
        for k in bt_only[:6]:
            a = bt_agg[k]
            print(f"  {a['station_id']:<7} {a['target_date']}  {a['side']:<3} "
                  f"strat={a.get('strategy')}  claimed={a['claimed_p']:.3f}  won={a['won']}  pnl=${a['pnl']:+.2f}")

    print("=" * 92)
    if args.json:
        print(json.dumps({
            "status": "ok",
            "window": window,
            "live_bets": live_n, "backtest_bets": bt_n,
            "matched": len(matched), "live_only": len(live_only), "backtest_only": len(bt_only),
            "fill_delta_mean": (float(pd.Series(fill_deltas).mean()) if fill_deltas else None),
            "outcome_agreement": (outcome_agree / outcome_known if outcome_known else None),
            "live_pnl_matched": round(live_pnl_m, 4), "backtest_pnl_matched": round(bt_pnl_m, 4),
        }))


if __name__ == "__main__":
    main()
