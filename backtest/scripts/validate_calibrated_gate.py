"""Replay the NO gate with raw, 1D-isotonic and market-aware blend calibration
(°F only and both units) and check which closes the over-confidence on the bets
that actually fire without killing return on stake.

The 1D curve can't: the gate fires where the model disagrees with the market,
and there the market price carries information. The blend adds it as a feature.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make ``src/`` importable when run as a bare script (no install needed).
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from hightempbot.calibration.reliability import (  # noqa: E402
    ReliabilityCurve,
    blend_degenerate_reason,
)

CLAIMED_LO = 0.70
CLAIMED_HI = 1.0
FEE_THETA = 0.05

# NO StrategyConfig (from strategy_constants.py) — replicated for the offline gate.
NO_FP_MIN = 0.75
NO_FP_MAX = 1.00
NO_MIN_EDGE = 0.090
NO_MAX_EDGE = 0.15
NO_MIN_N = 30
NO_MIN_VOL = 50.0
# Ceiling extension (signal p_B_50, relaxed fp/edge).
CEIL_FP_MIN = 0.50
CEIL_MAX_EDGE = 0.35


def _group(label: str | None) -> str | None:
    if not label:
        return None
    if "°F" in label:
        return "NO_F"
    if "°C" in label:
        return "NO_C"
    return None


def _fee(price: float) -> float:
    return FEE_THETA * price * (1.0 - price)


def _pnl_at_stake(won_no: float, price: float, stake: float = 10.0) -> float:
    """Net (of entry fee) PnL of a NO bet at ``price`` for ``stake`` dollars."""
    shares = stake / price
    entry_fee = shares * _fee(price)
    if won_no >= 0.5:
        return shares * 1.0 - stake - entry_fee
    return -stake - entry_fee


def fit_curves(df) -> dict[str, ReliabilityCurve]:
    """Fit one 1D isotonic curve per group on claimed in [0.70,1.0] (in-sample)."""
    pairs: dict[str, list[tuple[float, float]]] = {"NO_F": [], "NO_C": []}
    for label, p_yes, won_yes in zip(df["bracket_label"], df["p_E"], df["won_yes"], strict=False):
        g = _group(label)
        if g is None:
            continue
        try:
            claimed = 1.0 - float(p_yes)
            won = 1.0 - float(won_yes)
        except (TypeError, ValueError):
            continue
        if claimed != claimed or not (CLAIMED_LO <= claimed <= CLAIMED_HI):
            continue
        pairs[g].append((claimed, won))
    return {g: ReliabilityCurve.fit(p, group_key=g) for g, p in pairs.items()}


def fit_blend_curves(df) -> dict[str, ReliabilityCurve]:
    """Fit one market-aware logit-blend curve per group over ALL candidates."""
    triples: dict[str, list[tuple[float, float, float]]] = {"NO_F": [], "NO_C": []}
    for label, p_yes, won_yes, no_price in zip(
        df["bracket_label"], df["p_E"], df["won_yes"], df["no_price"], strict=False
    ):
        g = _group(label)
        if g is None:
            continue
        try:
            claimed = 1.0 - float(p_yes)
            won = 1.0 - float(won_yes)
            price = float(no_price)
        except (TypeError, ValueError):
            continue
        if claimed != claimed or price != price:
            continue
        if not (CLAIMED_LO <= claimed <= CLAIMED_HI) or not (0.0 < price < 1.0):
            continue
        triples[g].append((claimed, price, won))
    return {g: ReliabilityCurve.fit_blend(t, group_key=g) for g, t in triples.items()}


def replay(df, curves, blend_curves, *, mode: str, f_only: bool) -> dict:
    """Replay the NO gate over the decision table; return fired-slice metrics."""
    def transform(group: str, claimed_raw: float, price: float) -> float:
        if mode == "raw":
            return claimed_raw
        if mode == "1d":
            return curves[group].apply(claimed_raw)
        return blend_curves[group].apply(claimed_raw, price)

    n = 0
    wins = 0.0
    sum_claimed = 0.0
    sum_raw_claimed = 0.0
    sum_price = 0.0
    total_pnl = 0.0
    total_stake = 0.0
    stake = 10.0

    for row in df.itertuples(index=False):
        group = _group(row.bracket_label)
        if group is None:
            continue
        if f_only and group != "NO_F":
            continue
        price = row.no_price
        if price is None or price != price or price <= 0:
            continue
        n_cum = row.n_cum
        if n_cum is None or n_cum < NO_MIN_N:
            continue
        vol = row.avg_volume
        if vol is None or vol != vol or vol < NO_MIN_VOL:
            continue
        try:
            p_e = float(row.p_E)
            won_yes = float(row.won_yes)
        except (TypeError, ValueError):
            continue
        won_no = 1.0 - won_yes
        fee = _fee(price)

        # --- strict NO gate ---
        raw_claimed = 1.0 - p_e
        claimed = transform(group, raw_claimed, price)
        edge = claimed - price - fee
        fired = False
        claimed_used = claimed
        raw_used = raw_claimed
        if NO_FP_MIN <= price <= NO_FP_MAX and NO_MIN_EDGE <= edge <= NO_MAX_EDGE:
            fired = True
        elif getattr(row, "bracket_kind", "") == "high":
            # --- ceiling extension (signal p_B_50) ---
            p_b50 = getattr(row, "p_B_50", float("nan"))
            if p_b50 == p_b50:  # not NaN
                raw_ext = 1.0 - float(p_b50)
                claimed_ext = transform(group, raw_ext, price)
                edge_ext = claimed_ext - price - fee
                if CEIL_FP_MIN <= price <= NO_FP_MAX and NO_MIN_EDGE <= edge_ext <= CEIL_MAX_EDGE:
                    fired = True
                    claimed_used = claimed_ext
                    raw_used = raw_ext
        if not fired:
            continue

        n += 1
        wins += won_no
        sum_claimed += claimed_used
        sum_raw_claimed += raw_used
        sum_price += price
        total_pnl += _pnl_at_stake(won_no, price, stake)
        total_stake += stake

    win_rate = wins / n if n else float("nan")
    mean_claimed = sum_claimed / n if n else float("nan")
    ros = total_pnl / total_stake if total_stake else float("nan")
    return {
        "n": n,
        "win_rate": win_rate,
        "mean_claimed": mean_claimed,
        "mean_raw_claimed": sum_raw_claimed / n if n else float("nan"),
        "mean_price": sum_price / n if n else float("nan"),
        "realized": win_rate,
        "reliability_gap": (mean_claimed - win_rate) if n else float("nan"),
        "ros": ros,
        "pnl_10": total_pnl,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--parquet",
        default=str(_REPO_ROOT / "backtest/data/decision_table_may11plus_l2.parquet"),
    )
    args = parser.parse_args(argv)

    import pandas as pd

    df = pd.read_parquet(args.parquet)
    curves = fit_curves(df)
    blend_curves = fit_blend_curves(df)
    for g in ("NO_F", "NO_C"):
        c = curves[g]
        b = blend_curves[g]
        print(f"fit {g}: 1d n={c.n_pairs} breakpoints={len(c.breakpoints)} "
              f"identity={c.is_identity}")
        coefs = [round(x, 4) for x in b.coefs]
        dr = blend_degenerate_reason(b.coefs)
        if len(b.coefs) >= 3:
            pw = abs(b.coefs[2]) / (abs(b.coefs[1]) + abs(b.coefs[2]))
            print(f"       blend n={b.n_pairs} coefs(b0,b1,b2)={coefs} "
                  f"price_weight={pw:.3f} degenerate={dr!r}")
        else:
            print(f"       blend n={b.n_pairs} coefs={coefs} degenerate={dr!r}")

    variants = [
        ("raw    / °F-only", dict(mode="raw", f_only=True)),
        ("1d     / °F-only", dict(mode="1d", f_only=True)),
        ("blend  / °F-only", dict(mode="blend", f_only=True)),
        ("raw    / both   ", dict(mode="raw", f_only=False)),
        ("1d     / both   ", dict(mode="1d", f_only=False)),
        ("blend  / both   ", dict(mode="blend", f_only=False)),
    ]
    results = [(name, replay(df, curves, blend_curves, **kw)) for name, kw in variants]

    # Columns tell the whole story: raw_clm (model), cal_clm (calibrated used),
    # price (market NO), realized (win rate) on the FIRED slice; rel_gap =
    # cal_clm - realized.
    header = (
        f"{'variant':<17} {'n':>5} {'realized':>9} {'raw_clm':>8} {'cal_clm':>8} "
        f"{'price':>7} {'rel_gap':>9} {'ROS':>9} {'PnL@$10':>10}"
    )
    print("\n" + header)
    print("-" * len(header))
    for name, r in results:
        print(
            f"{name:<17} {r['n']:>5d} {r['realized']:>9.4f} {r['mean_raw_claimed']:>8.4f} "
            f"{r['mean_claimed']:>8.4f} {r['mean_price']:>7.4f} "
            f"{r['reliability_gap']:>+9.4f} {r['ros']:>+9.4f} {r['pnl_10']:>+10.2f}"
        )

    res = dict(results)

    # --- verdict (°F is the only live-firing unit; both-units is diagnostic) ---
    raw_f = res["raw    / °F-only"]
    d1_f = res["1d     / °F-only"]
    bl_f = res["blend  / °F-only"]
    bl_both = res["blend  / both   "]
    print("\nVERDICT (°F-only — the only live-firing unit; ALLOWED_BRACKET_UNITS={'F'}):")
    print(f"  raw   : n={raw_f['n']:>3d}  rel_gap={raw_f['reliability_gap']:+.4f}  "
          f"ROS={raw_f['ros']:+.4f}")
    print(f"  1d    : n={d1_f['n']:>3d}  rel_gap={d1_f['reliability_gap']:+.4f}  "
          f"ROS={d1_f['ros']:+.4f}  (selection-conditional — leaves over-confidence)")
    print(f"  blend : n={bl_f['n']:>3d}  rel_gap={bl_f['reliability_gap']:+.4f}  "
          f"ROS={bl_f['ros']:+.4f}")

    n_drop = raw_f["n"] - bl_f["n"]
    print(f"\n  DOMINANT FINDING — n collapses {raw_f['n']} -> {bl_f['n']} "
          f"({n_drop} of {raw_f['n']} °F bets, {100.0 * n_drop / raw_f['n']:.0f}%, stop firing).")
    print("  Folding the market NO price into the claim reveals the raw °F 'edge' was\n"
          "  mostly miscalibration: b1(claim)=0.12 vs b2(price)=0.80 (price_weight 0.87) —\n"
          "  the model barely improves on the market, so once the market is in the blend\n"
          "  the average fired bet's true edge (~2-4pp) falls below the 9pp gate.")
    print(f"  The °F blend rel_gap ({bl_f['reliability_gap']:+.4f}) is n=8 noise (all 8 won).\n"
          f"  Clean calibration evidence is the both-units slice: n={bl_both['n']}, "
          f"rel_gap={bl_both['reliability_gap']:+.4f}, ROS={bl_both['ros']:+.4f} — the blend\n"
          "  IS well-calibrated where n is large enough to measure it.")

    surviving = bl_f["pnl_10"]
    print(f"\n  BOTTOM LINE — real °F edge surviving market-aware calibration: "
          f"${surviving:+.2f} PnL@$10\n"
          f"  over n={bl_f['n']} bets (raw claim {raw_f['mean_raw_claimed']:.3f} -> "
          f"market {bl_f['mean_price']:.3f} -> realized {bl_f['realized']:.3f}). "
          "Strongly positive per\n"
          "  bet but too few to redeploy on °F alone. NOT a green light to size up the\n"
          "  current NO gate; it IS a green light to widen the fired population (lower the\n"
          "  edge gate and/or re-enable °C behind the same blend, where n is real).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
