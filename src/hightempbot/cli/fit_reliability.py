"""Fit reliability-calibration curves for the NO gate and persist them.

Fits one reliability curve per bracket-unit group ('NO_C' and 'NO_F'), mapping
the NO gate's *claimed* probability (``P(NO wins) = 1 - p_yes``) to the
*realized* win rate. The default — and what production's NO gate requires as
the active curve — is the market-aware logit blend::

    calibrated = sigmoid(b0 + b1*logit(claimed) + b2*logit(no_price))

The legacy 1D isotonic (pool-adjacent-violators) curve remains available via
``--curve-type isotonic``. The curves are written to the ``reliability_curves``
table of a target DB (deactivating any prior active curve per group — so
committing an isotonic curve REPLACES an active logit_blend curve) and a
reliability table (claimed vs realized vs n) is printed for each group.

Training data
-------------
Two sources, unioned per group:

  (a) The local backtest decision-table parquet (required). Pair-extraction
      rule (one pair per bracket row):
        * claimed = 1 - p_yes, using the ``p_E`` column (the model's YES
          probability). So claimed is the model's P(NO wins) for that bracket.
        * won     = 1 - won_yes  (NO wins iff the actual high did NOT land in
          the bracket).
        * group   = 'NO_F' if the bracket_label carries a °F unit token, else
          'NO_C' if it carries °C (rows with neither are dropped).
        * keep only rows with claimed in [0.70, 1.0] — the NO gate's operating
          regime (it needs fill_price >= 0.75, i.e. a high claimed prob).

  (b) Optionally, resolved NO ledger rows from a live DB (``--ledger-db``).
      For each ``side='NO'`` bet with a terminal WIN/LOSS outcome:
        * claimed = event_detail.claimed_raw when present (the raw pre-calibration
          claim), else prob_safe_floor (both are 1 - p_E at placement time).
        * won     = 1 if outcome == 'WIN' else 0.
        * group   = 'NO_F' / 'NO_C' from event_detail.bracket_unit.
        * same [0.70, 1.0] claimed restriction.

Usage
-----
    # CANONICAL: fit the market-aware logit blend (the default) and write the
    # curves into a DB — this is what production's NO gate requires.
    # A degenerate fit (price-dominant / wrong-signed) is reported but never
    # written — see validate_calibrated_gate.py for the honest fired-slice test.
    python -m hightempbot.cli.fit_reliability \
        --parquet backtest/data/decision_table_may11plus_l2.parquet \
        --db data/hightempbot.db --commit

    # dry-run: fit + print the reliability tables, do NOT write curves
    python -m hightempbot.cli.fit_reliability \
        --parquet backtest/data/decision_table_may11plus_l2.parquet

    # also fold in resolved live NO bets
    python -m hightempbot.cli.fit_reliability \
        --parquet backtest/data/decision_table_may11plus_l2.parquet \
        --ledger-db data/hightempbot.db --db data/hightempbot.db --commit

    # LEGACY: fit the 1D isotonic curve instead of the logit blend. Committing
    # it deactivates the group's active logit_blend curve (which production's
    # NO gate requires), so --commit emits a prominent warning.
    python -m hightempbot.cli.fit_reliability \
        --curve-type isotonic \
        --parquet backtest/data/decision_table_may11plus_l2.parquet \
        --db data/hightempbot.db --commit
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys

from hightempbot.calibration.reliability import (
    CURVE_TYPE_BLEND,
    CURVE_TYPE_ISOTONIC,
    MIN_PAIRS,
    NO_GROUPS,
    ReliabilityCurve,
    blend_degenerate_reason,
    ensure_reliability_table,
    save_curve,
)

logger = logging.getLogger(__name__)

CLAIMED_LO = 0.70
CLAIMED_HI = 1.0

# event_detail keys, in priority order, that may carry a live NO bet's entry
# market price for the market-aware blend fit.
_LEDGER_PRICE_KEYS = ("fill_price", "entry_top_price", "p_market", "no_price")


def _unit_group_from_label(label: str | None) -> str | None:
    """'NO_F' if the label carries °F, 'NO_C' if °C, else None."""
    if not label:
        return None
    if "°F" in label:
        return "NO_F"
    if "°C" in label:
        return "NO_C"
    return None


def pairs_from_parquet(parquet_path: str) -> dict[str, list[tuple[float, float]]]:
    """Extract (claimed, won) NO pairs per group from the decision table.

    See the module docstring for the exact extraction rule.
    """
    import pandas as pd

    df = pd.read_parquet(parquet_path)
    needed = {"p_E", "won_yes", "bracket_label"}
    missing = needed - set(df.columns)
    if missing:
        raise SystemExit(
            f"decision table {parquet_path} is missing required columns: {sorted(missing)}"
        )

    out: dict[str, list[tuple[float, float]]] = {g: [] for g in NO_GROUPS}
    for label, p_yes, won_yes in zip(
        df["bracket_label"], df["p_E"], df["won_yes"], strict=False
    ):
        group = _unit_group_from_label(label)
        if group is None:
            continue
        try:
            claimed = 1.0 - float(p_yes)
            won = 1.0 - float(won_yes)
        except (TypeError, ValueError):
            continue
        if claimed != claimed:  # NaN
            continue
        if not (CLAIMED_LO <= claimed <= CLAIMED_HI):
            continue
        out[group].append((claimed, won))
    return out


def pairs_from_ledger(ledger_db_path: str) -> dict[str, list[tuple[float, float]]]:
    """Extract (claimed, won) NO pairs per group from resolved live NO bets."""
    conn = sqlite3.connect(ledger_db_path)
    conn.row_factory = sqlite3.Row
    out: dict[str, list[tuple[float, float]]] = {g: [] for g in NO_GROUPS}
    try:
        rows = conn.execute(
            "SELECT outcome, prob_safe_floor, event_detail FROM ledger "
            "WHERE side = 'NO' AND event_type IN ('bet', 'dry_run') "
            "AND outcome IN ('WIN', 'LOSS')"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        try:
            detail = json.loads(row["event_detail"]) if row["event_detail"] else {}
        except (TypeError, json.JSONDecodeError):
            detail = {}
        unit = detail.get("bracket_unit")
        group = {"F": "NO_F", "C": "NO_C"}.get(str(unit) if unit else "")
        if group is None:
            continue
        claimed = detail.get("claimed_raw")
        if claimed is None:
            claimed = row["prob_safe_floor"]
        if claimed is None:
            continue
        try:
            claimed = float(claimed)
        except (TypeError, ValueError):
            continue
        if not (CLAIMED_LO <= claimed <= CLAIMED_HI):
            continue
        won = 1.0 if row["outcome"] == "WIN" else 0.0
        out[group].append((claimed, won))
    return out


def triples_from_parquet(parquet_path: str) -> dict[str, list[tuple[float, float, float]]]:
    """Extract ``(claimed, no_price, won)`` NO triples per group for the blend fit.

    Same claimed/won/group rule as :func:`pairs_from_parquet`, plus the entry
    market NO price (``no_price`` column). Rows with a missing/out-of-range price
    (<=0 or >=1) are dropped — ``logit(price)`` is only defined on the open unit
    interval and a boundary price carries no market signal.
    """
    import pandas as pd

    df = pd.read_parquet(parquet_path)
    needed = {"p_E", "won_yes", "bracket_label", "no_price"}
    missing = needed - set(df.columns)
    if missing:
        raise SystemExit(
            f"decision table {parquet_path} is missing required columns: {sorted(missing)}"
        )

    out: dict[str, list[tuple[float, float, float]]] = {g: [] for g in NO_GROUPS}
    for label, p_yes, won_yes, no_price in zip(
        df["bracket_label"], df["p_E"], df["won_yes"], df["no_price"], strict=False
    ):
        group = _unit_group_from_label(label)
        if group is None:
            continue
        try:
            claimed = 1.0 - float(p_yes)
            won = 1.0 - float(won_yes)
            price = float(no_price)
        except (TypeError, ValueError):
            continue
        if claimed != claimed or price != price:  # NaN
            continue
        if not (CLAIMED_LO <= claimed <= CLAIMED_HI):
            continue
        if not (0.0 < price < 1.0):
            continue
        out[group].append((claimed, price, won))
    return out


def triples_from_ledger(ledger_db_path: str) -> dict[str, list[tuple[float, float, float]]]:
    """Extract ``(claimed, no_price, won)`` triples from resolved live NO bets.

    Best-effort market price: read the first present of ``_LEDGER_PRICE_KEYS``
    from ``event_detail``. Rows without any usable entry price are dropped (the
    blend cannot use them); such rows still feed the isotonic fit via
    :func:`pairs_from_ledger`.
    """
    conn = sqlite3.connect(ledger_db_path)
    conn.row_factory = sqlite3.Row
    out: dict[str, list[tuple[float, float, float]]] = {g: [] for g in NO_GROUPS}
    try:
        rows = conn.execute(
            "SELECT outcome, prob_safe_floor, event_detail FROM ledger "
            "WHERE side = 'NO' AND event_type IN ('bet', 'dry_run') "
            "AND outcome IN ('WIN', 'LOSS')"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        try:
            detail = json.loads(row["event_detail"]) if row["event_detail"] else {}
        except (TypeError, json.JSONDecodeError):
            detail = {}
        unit = detail.get("bracket_unit")
        group = {"F": "NO_F", "C": "NO_C"}.get(str(unit) if unit else "")
        if group is None:
            continue
        claimed = detail.get("claimed_raw")
        if claimed is None:
            claimed = row["prob_safe_floor"]
        price = next(
            (detail[k] for k in _LEDGER_PRICE_KEYS if detail.get(k) is not None),
            None,
        )
        if claimed is None or price is None:
            continue
        try:
            claimed = float(claimed)
            price = float(price)
        except (TypeError, ValueError):
            continue
        if not (CLAIMED_LO <= claimed <= CLAIMED_HI):
            continue
        if not (0.0 < price < 1.0):
            continue
        won = 1.0 if row["outcome"] == "WIN" else 0.0
        out[group].append((claimed, price, won))
    return out


def reliability_table(pairs: list[tuple[float, float]], *, n_bins: int = 6) -> list[dict]:
    """Bin pairs by claimed and return per-bin (claimed_mean, realized, n).

    Bins span [CLAIMED_LO, CLAIMED_HI]; empty bins are omitted.
    """
    edges = [CLAIMED_LO + (CLAIMED_HI - CLAIMED_LO) * i / n_bins for i in range(n_bins + 1)]
    bins: list[list[float]] = [[0.0, 0.0, 0] for _ in range(n_bins)]  # sum_claimed, sum_won, n
    for claimed, won in pairs:
        idx = min(n_bins - 1, max(0, int((claimed - CLAIMED_LO) / (CLAIMED_HI - CLAIMED_LO) * n_bins)))
        bins[idx][0] += claimed
        bins[idx][1] += won
        bins[idx][2] += 1
    table: list[dict] = []
    for i, (sc, sw, n) in enumerate(bins):
        if n == 0:
            continue
        table.append({
            "bin": f"[{edges[i]:.3f},{edges[i + 1]:.3f})",
            "claimed": sc / n,
            "realized": sw / n,
            "n": n,
        })
    return table


def _print_group_report(group: str, pairs: list[tuple[float, float]], curve: ReliabilityCurve) -> None:
    n = len(pairs)
    overall_claimed = sum(c for c, _ in pairs) / n if n else float("nan")
    overall_realized = sum(w for _, w in pairs) / n if n else float("nan")
    print(f"\n=== {group}  (n_pairs={n}) ===")
    if n < MIN_PAIRS:
        print(f"  WARNING: n_pairs={n} < MIN_PAIRS={MIN_PAIRS}; curve will be refused at load (identity).")
    print(f"  overall: claimed={overall_claimed:.4f}  realized={overall_realized:.4f}  "
          f"gap={overall_claimed - overall_realized:+.4f}")
    print(f"  {'bin':<18} {'claimed':>9} {'realized':>9} {'calibrated':>11} {'n':>7}")
    for row in reliability_table(pairs):
        cal = curve.apply(row["claimed"])
        print(f"  {row['bin']:<18} {row['claimed']:>9.4f} {row['realized']:>9.4f} "
              f"{cal:>11.4f} {row['n']:>7d}")


def _print_blend_report(
    group: str, triples: list[tuple[float, float, float]], curve: ReliabilityCurve
) -> str | None:
    """Print a market-aware blend report. Returns a degenerate reason or None."""
    n = len(triples)
    claimed_mean = sum(t[0] for t in triples) / n if n else float("nan")
    price_mean = sum(t[1] for t in triples) / n if n else float("nan")
    realized = sum(t[2] for t in triples) / n if n else float("nan")
    print(f"\n=== {group}  (blend, n_triples={n}) ===")
    if n < MIN_PAIRS:
        print(f"  WARNING: n={n} < MIN_PAIRS={MIN_PAIRS}; curve will be refused at load (identity).")
    print(f"  overall: claimed={claimed_mean:.4f}  no_price={price_mean:.4f}  "
          f"realized={realized:.4f}")
    coefs = curve.coefs
    if len(coefs) < 3:
        print("  FIT FAILED: empty/degenerate coefficients (identity fallback).")
        return "empty coefs"
    b0, b1, b2 = coefs
    denom = abs(b1) + abs(b2)
    price_weight = abs(b2) / denom if denom else float("nan")
    print(f"  coefs: b0={b0:+.4f}  b1(claim)={b1:+.4f}  b2(price)={b2:+.4f}  "
          f"price_weight={price_weight:.3f}")
    reason = blend_degenerate_reason(coefs)
    if reason is not None:
        print(f"  DEGENERATE: {reason}")
        print("  -> curve will be refused at load; NOT written under --commit.")
    else:
        # Show the calibrated value at a few (claimed, price) probes.
        print(f"  {'claimed':>9} {'no_price':>9} {'calibrated':>11}")
        for c in (0.90, 0.95, 0.99):
            for pr in (0.75, 0.85):
                print(f"  {c:>9.4f} {pr:>9.4f} {curve.apply(c, pr):>11.4f}")
    return reason


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--parquet",
        default="backtest/data/decision_table_may11plus_l2.parquet",
        help="Path to the backtest decision-table parquet (training source a).",
    )
    parser.add_argument(
        "--ledger-db",
        default=None,
        help="Optional DB path to also fold in resolved live NO bets (source b).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Target DB to write fitted curves into. Required with --commit.",
    )
    parser.add_argument(
        "--curve-type",
        choices=(CURVE_TYPE_BLEND, CURVE_TYPE_ISOTONIC),
        default=CURVE_TYPE_BLEND,
        help=(
            "logit_blend (default; required active curve for production's NO "
            "gate): market-aware sigmoid(b0 + b1*logit(claimed) + "
            "b2*logit(no_price)) — estimates P(NO wins | claim, market price). "
            "isotonic (legacy): 1D claimed->realized PAV curve; committing it "
            "REPLACES the group's active logit_blend curve."
        ),
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Write fitted curves to --db. Default is dry-run (print tables only).",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.commit and not args.db:
        parser.error("--commit requires --db (target DB to write curves into)")

    if args.curve_type == CURVE_TYPE_BLEND:
        return _run_blend(args)

    # --- Isotonic (legacy) ---
    if args.commit:
        banner = (
            "!" * 74 + "\n"
            "! WARNING: committing LEGACY isotonic curves.\n"
            "! Production's NO gate requires an ACTIVE logit_blend curve, and\n"
            "! save_curve deactivates the previous active curve for each group —\n"
            "! this commit will REPLACE any active logit_blend curve with isotonic.\n"
            "! If that is not intended, rerun without --curve-type isotonic\n"
            "! (logit_blend is the default).\n"
            + "!" * 74
        )
        print(banner, file=sys.stderr)
        logger.warning(
            "isotonic --commit: committed isotonic curves will deactivate the "
            "active logit_blend curve(s) production's NO gate requires"
        )

    # Gather pairs.
    pairs_by_group = pairs_from_parquet(args.parquet)
    if args.ledger_db:
        ledger_pairs = pairs_from_ledger(args.ledger_db)
        for group in NO_GROUPS:
            pairs_by_group[group].extend(ledger_pairs.get(group, []))

    # Fit + report each group.
    curves: dict[str, ReliabilityCurve] = {}
    for group in NO_GROUPS:
        pairs = pairs_by_group[group]
        curve = ReliabilityCurve.fit(pairs, group_key=group)
        curves[group] = curve
        _print_group_report(group, pairs, curve)

    if not args.commit:
        print("\n(dry-run: no curves written; pass --commit --db <path> to persist)")
        return 0

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        ensure_reliability_table(conn)
        for group in NO_GROUPS:
            curve = curves[group]
            if curve.n_pairs < MIN_PAIRS:
                print(f"SKIP write {group}: n_pairs={curve.n_pairs} < MIN_PAIRS={MIN_PAIRS}")
                continue
            row_id = save_curve(conn, curve)
            print(f"wrote {group} curve (id={row_id}, n_pairs={curve.n_pairs}, "
                  f"breakpoints={len(curve.breakpoints)})")
    finally:
        conn.close()
    return 0


def _run_blend(args) -> int:
    """Fit + report + optionally persist market-aware logit-blend curves.

    A degenerate fit (see :func:`blend_degenerate_reason`) is reported but NOT
    written under ``--commit`` — shipping a curve that collapses the edge to ~0
    would silently halt NO trading, so it is surfaced to the operator instead.
    """
    triples_by_group = triples_from_parquet(args.parquet)
    if args.ledger_db:
        ledger_triples = triples_from_ledger(args.ledger_db)
        for group in NO_GROUPS:
            triples_by_group[group].extend(ledger_triples.get(group, []))

    curves: dict[str, ReliabilityCurve] = {}
    reasons: dict[str, str | None] = {}
    for group in NO_GROUPS:
        triples = triples_by_group[group]
        curve = ReliabilityCurve.fit_blend(triples, group_key=group)
        curves[group] = curve
        reasons[group] = _print_blend_report(group, triples, curve)

    if not args.commit:
        print("\n(dry-run: no curves written; pass --commit --db <path> to persist)")
        return 0

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        ensure_reliability_table(conn)
        for group in NO_GROUPS:
            curve = curves[group]
            if curve.n_pairs < MIN_PAIRS:
                print(f"SKIP write {group}: n={curve.n_pairs} < MIN_PAIRS={MIN_PAIRS}")
                continue
            if reasons[group] is not None:
                print(f"SKIP write {group}: degenerate blend ({reasons[group]}); not shipping.")
                continue
            row_id = save_curve(conn, curve)
            print(f"wrote {group} blend curve (id={row_id}, n={curve.n_pairs}, "
                  f"coefs={[round(c, 4) for c in curve.coefs]})")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
