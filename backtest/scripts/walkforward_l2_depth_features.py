"""Walk-forward ABCD selection for L2-depth variants.

This consumes backtest/results/l2_depth_feature_sweep.csv and performs an
expanding walk-forward:

    train A     -> select variant for B
    train A+B   -> select variant for C
    train A+B+C -> select variant for D

Chunk A is training-only in this protocol. Reporting A as out-of-sample would
leak, because there is no earlier chunk to select from.
"""

from __future__ import annotations

import csv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
IN_CSV = REPO_ROOT / "backtest" / "results" / "l2_depth_feature_sweep.csv"
OUT_MD = REPO_ROOT / "backtest" / "results" / "l2_depth_walkforward_abcd.md"
OUT_CSV = REPO_ROOT / "backtest" / "results" / "l2_depth_walkforward_abcd.csv"

CHUNKS = ("A", "B", "C", "D")
RECOMMENDED_PROFILE = "l2_tail_book_no_no_retune"


def _f(row: dict, key: str) -> float:
    return float(row.get(key) or 0.0)


def _i(row: dict, key: str) -> int:
    return int(float(row.get(key) or 0))


def _stats(row: dict, chunks: tuple[str, ...]) -> tuple[float, float, int, float, int]:
    pnls = [_f(row, f"{c}_pnl") for c in chunks]
    dds = [_f(row, f"{c}_dd") for c in chunks]
    ns = [_i(row, f"{c}_n") for c in chunks]
    positive = sum(1 for p in pnls if p > 0)
    worst = min(pnls) if pnls else 0.0
    total = sum(pnls)
    max_dd = max(dds) if dds else 0.0
    n_total = sum(ns)
    return total, worst, positive, max_dd, n_total


def train_key(row: dict, train_chunks: tuple[str, ...], selector_rule: str) -> tuple:
    total, worst, positive, max_dd, n_total = _stats(row, train_chunks)
    if selector_rule == "risk_adjusted":
        return (positive, total / max(max_dd, 1e-9), worst, total, -n_total)
    return (positive, worst, total, -max_dd, -n_total)


def _profile_rows(rows: list[dict], profile: str) -> list[dict]:
    if profile == "expanded_all":
        return rows
    if profile == "original_178":
        return [
            row for row in rows
            if not row["variant"].startswith("tailh1_ref_") and not row["variant"].startswith("sel_")
        ]
    if profile == "l2_exec_shape":
        blocked = ("sel_no", "sel_taila", "sel_tail_", "sel_bid", "sel_spread")
        return [row for row in rows if not row["variant"].startswith(blocked)]
    if profile == RECOMMENDED_PROFILE:
        return [row for row in rows if not row["variant"].startswith("sel_no")]
    raise ValueError(f"unknown profile: {profile}")


def _profile_description(profile: str) -> str:
    return {
        "expanded_all": "All variants, including exploratory NO gate retunes.",
        "original_178": "Original L2 feature set before the final expansion.",
        "l2_exec_shape": "L2 execution/hour/shape variants, excluding final tail-param and NO-specific retunes.",
        RECOMMENDED_PROFILE: "Tail/book-depth L2 expansion, excluding NO-side retunes that overfit chunk A.",
    }[profile]


def _select_profile(rows: list[dict], profile: str, selector_rule: str) -> list[dict]:
    subset = _profile_rows(rows, profile)
    if not subset:
        raise ValueError(f"profile {profile} selected zero rows")

    selected: list[dict] = []
    for idx, test_chunk in enumerate(CHUNKS[1:], start=1):
        train_chunks = CHUNKS[:idx]
        winner = max(subset, key=lambda row: train_key(row, train_chunks, selector_rule))
        selected.append({
            "profile": profile,
            "profile_description": _profile_description(profile),
            "selector_rule": selector_rule,
            "step": f"{'+'.join(train_chunks)}->{test_chunk}",
            "train_chunks": "+".join(train_chunks),
            "test_chunk": test_chunk,
            "variant": winner["variant"],
            "description": winner["description"],
            "train_total": round(sum(_f(winner, f"{c}_pnl") for c in train_chunks), 4),
            "train_worst": round(min(_f(winner, f"{c}_pnl") for c in train_chunks), 4),
            "train_positive": sum(1 for c in train_chunks if _f(winner, f"{c}_pnl") > 0),
            "train_max_dd": round(max(_f(winner, f"{c}_dd") for c in train_chunks), 4),
            "test_pnl": round(_f(winner, f"{test_chunk}_pnl"), 4),
            "test_dd": round(_f(winner, f"{test_chunk}_dd"), 4),
            "test_n": _i(winner, f"{test_chunk}_n"),
            "test_no_pnl": round(_f(winner, f"{test_chunk}_no_pnl"), 4),
            "test_tail_pnl": round(_f(winner, f"{test_chunk}_tail_pnl"), 4),
        })

    final_winner = max(subset, key=lambda row: train_key(row, CHUNKS, selector_rule))
    for row in selected:
        row["final_variant"] = final_winner["variant"]
        row["final_description"] = final_winner["description"]
        row["final_total_pnl"] = round(_f(final_winner, "total_pnl"), 4)
        row["final_worst_chunk_pnl"] = round(_f(final_winner, "worst_chunk_pnl"), 4)
        row["final_max_chunk_dd"] = round(_f(final_winner, "max_chunk_dd"), 4)
        row["final_total_n"] = _i(final_winner, "total_n")
    return selected


def _summary(rows: list[dict]) -> dict:
    return {
        "test_total": round(sum(float(row["test_pnl"]) for row in rows), 4),
        "worst": round(min(float(row["test_pnl"]) for row in rows), 4),
        "pos": sum(1 for row in rows if float(row["test_pnl"]) > 0),
        "max_dd": round(max(float(row["test_dd"]) for row in rows), 4),
        "n": sum(int(row["test_n"]) for row in rows),
        "no_pnl": round(sum(float(row["test_no_pnl"]) for row in rows), 4),
        "tail_pnl": round(sum(float(row["test_tail_pnl"]) for row in rows), 4),
    }


def run() -> list[dict]:
    with IN_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    profile_specs = (
        ("expanded_all", "current"),
        ("expanded_all", "risk_adjusted"),
        ("original_178", "current"),
        ("l2_exec_shape", "current"),
        (RECOMMENDED_PROFILE, "risk_adjusted"),
    )
    selected: list[dict] = []
    for profile, selector_rule in profile_specs:
        selected.extend(_select_profile(rows, profile, selector_rule))

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = list(selected[0].keys())
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(selected)

    groups: dict[tuple[str, str], list[dict]] = {}
    for row in selected:
        groups.setdefault((row["profile"], row["selector_rule"]), []).append(row)
    recommended_rows = groups[(RECOMMENDED_PROFILE, "risk_adjusted")]
    recommended = _summary(recommended_rows)

    lines = [
        "# L2 Depth Walk-Forward ABCD",
        "",
        "Protocol: expanding walk-forward. Chunk A is training-only; B/C/D are out-of-sample test chunks.",
        "",
        f"Input sweep: `{IN_CSV}`",
        "",
        "## Summary By Selector",
        "",
        "| profile | selector | test total | worst test chunk | +chunks | max test DD | bets | NO | TAIL | final trained variant |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for (profile, selector_rule), group_rows in groups.items():
        s = _summary(group_rows)
        final_variant = group_rows[0]["final_variant"]
        marker = " (recommended)" if profile == RECOMMENDED_PROFILE and selector_rule == "risk_adjusted" else ""
        lines.append(
            f"| {profile}{marker} | {selector_rule} | ${s['test_total']:+.2f} | "
            f"${s['worst']:+.2f} | {s['pos']}/3 | {s['max_dd']:.2f}% | {s['n']} | "
            f"${s['no_pnl']:+.2f} | ${s['tail_pnl']:+.2f} | {final_variant} |"
        )
    lines += [
        "",
        "## Recommended Profile",
        "",
        (
            f"`{RECOMMENDED_PROFILE}` excludes NO-side retunes because the expanded "
            "NO grid overfit chunk A and failed the next chunk. It keeps the new "
            "L2 book/depth and TAIL refinements, then ranks train candidates by "
            "PnL divided by train max drawdown."
        ),
        "",
        (
            f"Recommended strict OOS: ${recommended['test_total']:+.2f}, "
            f"worst chunk ${recommended['worst']:+.2f}, max DD {recommended['max_dd']:.2f}%. "
            f"Final trained variant for forward use: `{recommended_rows[0]['final_variant']}`."
        ),
        "",
        "## Steps",
        "",
        "| profile | selector | step | selected variant | train total | train worst | train +chunks | train maxDD | test pnl | test DD | n |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in selected:
        lines.append(
            f"| {row['profile']} | {row['selector_rule']} | {row['step']} | {row['variant']} | "
            f"${float(row['train_total']):+.2f} | ${float(row['train_worst']):+.2f} | "
            f"{row['train_positive']}/{len(row['train_chunks'].split('+'))} | "
            f"{float(row['train_max_dd']):.2f}% | ${float(row['test_pnl']):+.2f} | "
            f"{float(row['test_dd']):.2f}% | {row['test_n']} |"
        )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return selected


def main() -> None:
    rows = run()
    recommended = [
        row for row in rows
        if row["profile"] == RECOMMENDED_PROFILE and row["selector_rule"] == "risk_adjusted"
    ]
    print({
        "steps": len(rows),
        "out_csv": str(OUT_CSV),
        "out_md": str(OUT_MD),
        "recommended_profile": RECOMMENDED_PROFILE,
        "recommended_test_total": round(sum(float(row["test_pnl"]) for row in recommended), 4),
        "recommended_final_variant": recommended[0]["final_variant"],
    })


if __name__ == "__main__":
    main()
