"""Champion honest-report + per-bet persistence driver (Phase 2, 2026-07-16).

Replays the L2-depth champion (candidate_l2_depth.json, the committed
`sel_taila40_fp03_cs40` profile) over the four chronological chunks A/B/C/D at
the ranking bankroll and reports, alongside PnL:

  - n bets, win rate +/- SE
  - mean claimed P(win) vs realized frequency (overconfidence, pp)
  - per-stake ROS
  - reliability table (claimed bin x realized freq x n)
  - the C-vs-F asymmetry (bracket-unit split)

It also PERSISTS the per-bet stream so live-vs-backtest joins are possible
(scripts/shadow_replay.py). Fixed = A-D; OOS = B-D.

Why this exists: `measure_tp_sl.py` uses a Feb-Apr / Apr-May TRAIN/TEST split.
The champion headline (+$464 fixed / +$252 OOS) came from the L2-depth ABCD
chunk replay, so this driver reproduces THAT harness while adding honest metrics.

Usage:
    python backtest/scripts/champion_honest_report.py            # champion as configured
    python backtest/scripts/champion_honest_report.py --immediate-tail   # committed methodology
    HTB_DUMP_BETS=0 python backtest/scripts/champion_honest_report.py     # skip parquet dump
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backtest.lib import honest_report as hr  # noqa: E402
from backtest.lib.live_match_eval import (  # noqa: E402
    apply_no_tail_conflict_policy, candidates_3strats,
)
from backtest.scripts.lut_range_chunks import date_chunks, tp_sl_from_config  # noqa: E402
from backtest.scripts.measure_tp_sl import (  # noqa: E402
    RANKING_BR, _candidate_kwargs, _live_config_from_spec, _no_tail_conflict_config,
    _size_fracs, _stake_caps_for_bankroll, _tail_delayed_entry_threshold,
    apply_tail_delayed_entry, load_metrics_by_slug, load_prices_by_slug_side, simulate,
)
from backtest.scripts.sweep_l2_depth_features import MARKET_DB  # noqa: E402

DEFAULT_PARQUET = REPO_ROOT / "backtest" / "data" / "decision_table_may11plus_l2.parquet"
DEFAULT_CONFIG = REPO_ROOT / "backtest" / "configs" / "candidate_l2_depth.json"
BETS_DIR = REPO_ROOT / "backtest" / "results" / "bets"

# The committed champion headline these numbers are compared against.
COMMITTED_FIXED_PNL = 464.46
COMMITTED_OOS_PNL = 251.96


def _df_records(df: pd.DataFrame) -> list[dict]:
    cols = ["market_slug", "entry_ts_unix", "close_ts_unix"]
    for hour in range(24):
        cols += [f"entry_ts_h{hour}", f"yes_ask_ladder_h{hour}", f"no_ask_ladder_h{hour}"]
    return df[[c for c in cols if c in df.columns]].to_dict("records")


def build_champion_bets(df, config, df_records, prices_by_ss, metrics_idx, *, apply_delay: bool):
    cfg = _live_config_from_spec(config, RANKING_BR)
    bets = candidates_3strats(df, cfg, **_candidate_kwargs(config))
    policy, scope = _no_tail_conflict_config(config)
    bets, conflict = apply_no_tail_conflict_policy(bets, df, policy=policy, scope=scope)
    threshold = _tail_delayed_entry_threshold(config) if apply_delay else None
    bets, delay_stats = apply_tail_delayed_entry(
        bets=bets, records=df_records, prices_by_ss=prices_by_ss,
        metrics_idx=metrics_idx, threshold=threshold,
    )
    bets.sort(key=lambda b: (b[0], b[9] if len(b) >= 10 else 0, b[1], b[8]))
    return bets, conflict, delay_stats


def replay(bets, prices_by_ss, metrics_idx, df_records, cfg, tp_sl, size_frac,
           max_stake, max_l2_ask_premium, chunks):
    """Reset-$100 per-chunk replay; returns per-chunk results + full bet_log."""
    chunk_results: dict[str, dict] = {}
    all_bets: list[dict] = []
    for ch in chunks:
        log: list[dict] = []
        r = simulate(bets, prices_by_ss, metrics_idx, df_records, cfg, tp_sl,
                     ch.start, ch.end, size_frac=size_frac, max_stake=max_stake,
                     max_l2_ask_premium=max_l2_ask_premium, bet_log=log)
        for rec in log:
            rec["period"] = ch.label
        chunk_results[ch.label] = {"pnl": r["pnl"], "n": r["n"],
                                   "dd_pct": r["max_dd_pct_peak"] * 100.0}
        all_bets.extend(log)
    return chunk_results, all_bets


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--parquet", type=Path, default=DEFAULT_PARQUET)
    ap.add_argument("--immediate-tail", action="store_true",
                    help="disable delayed-TAIL entry (reproduces the committed pre-delay methodology)")
    ap.add_argument("--no-dump", action="store_true", help="skip per-bet parquet dump")
    args = ap.parse_args()

    t0 = time.time()
    config = json.loads(args.config.read_text())
    df = pd.read_parquet(args.parquet)
    df_records = _df_records(df)
    chunks = date_chunks(df["market_date"], 4)
    prices_by_ss = load_prices_by_slug_side(MARKET_DB)
    metrics_idx = load_metrics_by_slug(MARKET_DB)
    print(f"[load] {round(time.time()-t0,1)}s  rows={len(df)}  chunks={[c.label for c in chunks]}",
          file=sys.stderr)

    apply_delay = not args.immediate_tail
    bets, conflict, delay_stats = build_champion_bets(
        df, config, df_records, prices_by_ss, metrics_idx, apply_delay=apply_delay)
    cfg = _live_config_from_spec(config, RANKING_BR)
    tp_sl = tp_sl_from_config(config)
    size_frac = _size_fracs(config)
    max_stake = _stake_caps_for_bankroll(config, RANKING_BR)
    max_l2_ask_premium = (
        float(config["max_l2_ask_premium"]) if config.get("max_l2_ask_premium") is not None else None
    )

    chunk_results, all_bets = replay(
        bets, prices_by_ss, metrics_idx, df_records, cfg, tp_sl, size_frac,
        max_stake, max_l2_ask_premium, chunks)
    hr.attach_meta(all_bets, df)

    fixed_labels = [c.label for c in chunks]
    oos_labels = [c.label for c in chunks[1:]]
    fixed_bets = [b for b in all_bets if b["period"] in fixed_labels]
    oos_bets = [b for b in all_bets if b["period"] in oos_labels]
    fixed_pnl = sum(cr["pnl"] for lab, cr in chunk_results.items() if lab in fixed_labels)
    oos_pnl = sum(cr["pnl"] for lab, cr in chunk_results.items() if lab in oos_labels)
    fixed_dd = max((cr["dd_pct"] for lab, cr in chunk_results.items() if lab in fixed_labels), default=0.0)
    oos_dd = max((cr["dd_pct"] for lab, cr in chunk_results.items() if lab in oos_labels), default=0.0)

    fixed_summary = hr.summarize(fixed_bets, portfolio_dd_pct=fixed_dd)
    oos_summary = hr.summarize(oos_bets, portfolio_dd_pct=oos_dd)

    mode = "IMMEDIATE-TAIL (committed methodology)" if args.immediate_tail else "DELAYED-TAIL (as configured / live)"
    print("\n" + "#" * 100)
    print(f"CHAMPION HONEST REPORT - {args.config.name} - {mode}")
    print("#" * 100)
    print("Chunks:")
    for c in chunks:
        cr = chunk_results[c.label]
        print(f"  {c.label}: {c.start}..{c.end}   pnl=${cr['pnl']:>+9.2f}  n={cr['n']:>3}  chunkDD={cr['dd_pct']:.1f}%")
    print(f"\nFIXED (A-D)  total=${fixed_pnl:+.2f}  n={len(fixed_bets)}   "
          f"[committed headline ${COMMITTED_FIXED_PNL:+.2f}]")
    print(f"OOS   (B-D)  total=${oos_pnl:+.2f}  n={len(oos_bets)}   "
          f"[committed headline ${COMMITTED_OOS_PNL:+.2f}]")
    if abs(fixed_pnl - COMMITTED_FIXED_PNL) > 0.05 * max(abs(COMMITTED_FIXED_PNL), 1):
        print(f"  ** WARNING: fixed PnL differs from the committed headline by "
              f"${fixed_pnl - COMMITTED_FIXED_PNL:+.2f} "
              f"({(fixed_pnl/COMMITTED_FIXED_PNL - 1)*100:+.0f}%). "
              f"The published number does NOT reproduce on current HEAD.")
    print()
    print(hr.render_text(fixed_summary, f"FIXED replay A-D (reset $100/chunk) - {mode}"))
    print()
    print(hr.render_text(oos_summary, f"OOS replay B-D (reset $100/chunk) - {mode}"))

    dump = not args.no_dump and os.environ.get("HTB_DUMP_BETS", "1").strip().lower() not in ("0", "false", "no", "")
    dumped = {}
    if dump:
        suffix = "immediate" if args.immediate_tail else "delayed"
        p1 = hr.persist_bets(fixed_bets, BETS_DIR / f"champion_l2_{suffix}_fixed.parquet")
        p2 = hr.persist_bets(oos_bets, BETS_DIR / f"champion_l2_{suffix}_oos_bd.parquet")
        dumped = {"fixed": str(p1), "oos_bd": str(p2)}
        print(f"\n[dump] per-bet streams: {dumped}")

    # Single-line JSON footer for programmatic consumers.
    print("\n" + json.dumps({
        "mode": mode,
        "fixed_pnl": round(fixed_pnl, 4), "fixed_n": len(fixed_bets),
        "oos_pnl": round(oos_pnl, 4), "oos_n": len(oos_bets),
        "committed_fixed_pnl": COMMITTED_FIXED_PNL, "committed_oos_pnl": COMMITTED_OOS_PNL,
        "fixed_overall": {k: fixed_summary["overall"][k] for k in
                          ("n", "win_rate", "win_rate_se", "claimed_mean", "realized_mean",
                           "overconfidence_pp", "ros_pct")},
        "fixed_by_unit": {u: {k: m[k] for k in ("n", "win_rate", "overconfidence_pp", "ros_pct", "pnl")}
                          for u, m in fixed_summary["by_unit"].items()},
        "delay_stats": delay_stats,
        "dumped": dumped,
        "elapsed_s": round(time.time() - t0, 1),
    }))


if __name__ == "__main__":
    main()
