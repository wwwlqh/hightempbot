"""Build the leakage-safe decision table (walk-forward EMOS + LUT), asserting
no leakage before writing ``backtest/data/decision_table_may11plus.parquet``.

    python backtest/scripts/build_decision_table.py
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backtest.lib.sweep_lib import (  # noqa: E402
    ENTRY_LOCAL_HOURS, LUT_RECENCY_HALF_LIVES,
    LIVE_DB, MARKET_DB, PARQUET_OUT,
    add_signal_flavors, assert_no_leakage,
    emos_for_rows, emos_p_in_bracket,
    load_actuals, load_ensembles, load_entry_prices, load_entry_prices_by_local_hour,
    load_station_timezones, load_walk_forward_emos, load_walk_forward_lut,
    lut_lookup_for_rows, parse_bracket, predict_emos,
)

logger = logging.getLogger("build_decision_table")

START_DATE = "2026-02-04"
END_DATE = "2026-05-11"
HOURS_BEFORE_CLOSE = 4


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    # mtime cache: skip rebuild if parquet is newer than both source DBs
    if PARQUET_OUT.exists():
        out_mtime = PARQUET_OUT.stat().st_mtime
        live_mtime = LIVE_DB.stat().st_mtime if LIVE_DB.exists() else 0
        mkt_mtime = MARKET_DB.stat().st_mtime if MARKET_DB.exists() else 0
        if out_mtime >= max(live_mtime, mkt_mtime):
            logger.info("%s up to date (mtime cache hit), skipping rebuild", PARQUET_OUT)
            df = pd.read_parquet(PARQUET_OUT)
            logger.info("loaded %d rows", len(df))
            return df

    logger.info("loading live DB primitives ...")
    live = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    actuals = load_actuals(live)
    ens = load_ensembles(live, START_DATE, END_DATE)
    emos_hist = load_walk_forward_emos(live)
    lut_cum = load_walk_forward_lut(live)
    station_tz = load_station_timezones(live)
    live.close()
    logger.info("  actuals=%d ensembles=%d emos_hist=%d lut_cum=%d station_tz=%d",
                len(actuals), len(ens), len(emos_hist), len(lut_cum), len(station_tz))

    logger.info("loading market DB primitives ...")
    mkt = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True)
    entries = load_entry_prices(mkt, hours_before_close=HOURS_BEFORE_CLOSE)
    logger.info("  loading per-local-hour entries (hours=%s)...", ENTRY_LOCAL_HOURS)
    entries_by_h = load_entry_prices_by_local_hour(mkt, station_tz, ENTRY_LOCAL_HOURS)
    logger.info("  entries=%d per-hour=%d", len(entries), len(entries_by_h))
    market_rows = mkt.execute("""
        SELECT market_slug, market_date, station_id, bracket_index, bracket_label,
               yes_token_id, no_token_id
        FROM markets
    """).fetchall()
    mkt.close()
    logger.info("  markets=%d entries=%d", len(market_rows), len(entries))

    # Step 1 — assemble base rows (filter to those with all required pieces)
    rows = []
    skipped = {"out_of_window": 0, "no_ensemble": 0, "no_actual": 0,
               "no_price": 0, "bad_bracket": 0}
    for slug, mdate, station, bidx, blabel, ytok, ntok in market_rows:
        if mdate < START_DATE or mdate > END_DATE:
            skipped["out_of_window"] += 1; continue
        if (station, mdate) not in ens:
            skipped["no_ensemble"] += 1; continue
        if (station, mdate) not in actuals:
            skipped["no_actual"] += 1; continue
        ent = entries.get(slug)
        if not ent or ent.get("yes_price") is None or ent.get("no_price") is None:
            skipped["no_price"] += 1; continue
        b = parse_bracket(blabel)
        if b is None:
            skipped["bad_bracket"] += 1; continue
        lo, hi, kind = b
        row = {
            "market_slug": slug,
            "station_id": station,
            "market_date": mdate,
            "bracket_index": bidx,
            "bracket_label": blabel,
            "bracket_kind": kind,
            "lo_c": lo,
            "hi_c": hi,
            "yes_token_id": ytok,
            "no_token_id": ntok,
            "yes_price": ent["yes_price"],
            "no_price": ent["no_price"],
            "entry_ts_unix": int(ent.get("yes_ts") or ent.get("no_ts") or 0),
            "avg_volume": ent.get("avg_volume"),
            "avg_liquidity": ent.get("avg_liquidity"),
            "avg_spread": ent.get("avg_spread"),
            # Per-snapshot-at-entry metrics (more realistic than lifetime averages)
            "entry_volume": ent.get("entry_volume"),
            "entry_liquidity": ent.get("entry_liquidity"),
            "entry_spread": ent.get("entry_spread"),
            "entry_metrics_ts": ent.get("entry_metrics_ts"),
            "actual_high_c": actuals[(station, mdate)],
        }
        # Attach per-local-hour prices (yes_price_h{H}, no_price_h{H}, entry_ts_h{H})
        h_entries = entries_by_h.get(slug, {})
        for H in ENTRY_LOCAL_HOURS:
            row[f"yes_price_h{H}"] = h_entries.get(f"yes_price_h{H}")
            row[f"no_price_h{H}"] = h_entries.get(f"no_price_h{H}")
            row[f"entry_ts_h{H}"] = h_entries.get(f"entry_ts_h{H}")
            row[f"entry_volume_h{H}"] = h_entries.get(f"entry_volume_h{H}")
            row[f"entry_liquidity_h{H}"] = h_entries.get(f"entry_liquidity_h{H}")
            row[f"entry_spread_h{H}"] = h_entries.get(f"entry_spread_h{H}")
            row[f"entry_metrics_ts_h{H}"] = h_entries.get(f"entry_metrics_ts_h{H}")
        rows.append(row)
    logger.info("  base rows=%d  skipped=%s", len(rows), skipped)

    df = pd.DataFrame(rows)

    # Step 2 — attach as-of EMOS (a, b, c, d) via merge_asof on station, market_date
    logger.info("merging walk-forward EMOS ...")
    df = emos_for_rows(df, emos_hist)
    if df["a"].isna().any():
        n_drop = int(df["a"].isna().sum())
        df = df[df["a"].notna()].reset_index(drop=True)
        logger.info("  dropped %d rows with no as-of EMOS (cold-start stations)", n_drop)

    # Step 3 — compute p_raw (EMOS Gaussian P(temp ∈ bracket)) per row
    logger.info("computing p_raw from EMOS ...")
    p_raws = []
    mus = []
    sigmas = []
    for r in df.itertuples():
        ensemble = ens[(r.station_id, r.market_date)]
        mu, sigma = predict_emos(r.a, r.b, r.c, r.d, ensemble)
        p_raws.append(emos_p_in_bracket(mu, sigma, r.lo_c, r.hi_c))
        mus.append(mu)
        sigmas.append(sigma)
    df["p_raw"] = p_raws
    # Keep mu/sigma for PIT and CRPS.
    df["emos_mu"] = mus
    df["emos_sigma"] = sigmas
    df["p_raw_for_bucket"] = df["p_raw"]  # alias for lut_lookup_for_rows

    # Step 4 — attach walk-forward LUT (n, hits) per row
    logger.info("merging walk-forward LUT ...")
    df = lut_lookup_for_rows(df, lut_cum)
    df["n_cum"] = df["n_cum"].fillna(0).astype(int)
    df["hits_cum"] = df["hits_cum"].fillna(0).astype(int)

    # Step 5 — settle outcomes
    df["won_yes"] = (
        (df["actual_high_c"] >= df["lo_c"]) & (df["actual_high_c"] < df["hi_c"])
    ).astype(int)

    # Step 6 — close timestamp + signal flavors
    df["close_ts_unix"] = df["market_date"].apply(
        lambda md: int(datetime.fromisoformat(md + "T00:00:00+00:00").timestamp()) + 86400
    )
    df = add_signal_flavors(df)

    # Step 7 — leakage asserts (raises on violation)
    logger.info("running leakage asserts ...")
    rep = assert_no_leakage(df, hours_before_close=HOURS_BEFORE_CLOSE)
    logger.info("  PASS — %d rows, 0 emos/lut/entry violations", rep.n_rows)

    # Step 8 — write parquet (drop intermediate cols not needed downstream)
    drop_cols = ["market_date_dt", "p_raw_for_bucket", "pred_bucket_low"]
    # Intermediate recency weight sums: p_Recency_h{hl} is materialized; the raw
    # w{hl}/wh{hl} accumulators are not needed downstream.
    drop_cols += [f"w{hl}" for hl in LUT_RECENCY_HALF_LIVES]
    drop_cols += [f"wh{hl}" for hl in LUT_RECENCY_HALF_LIVES]
    df = df.drop(columns=[c for c in drop_cols if c in df.columns])
    PARQUET_OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(PARQUET_OUT, index=False)
    logger.info("wrote %s — %d rows, %d cols", PARQUET_OUT, len(df), len(df.columns))

    # Quick stats
    logger.info("date range: %s -> %s, stations: %d, brackets/day median: %d",
                df["market_date"].min(), df["market_date"].max(),
                df["station_id"].nunique(),
                int(df.groupby(["station_id", "market_date"]).size().median()))
    logger.info("p_raw  mean=%.3f  std=%.3f", df["p_raw"].mean(), df["p_raw"].std())
    logger.info("n_cum  median=%d  pct_with_lut(n>=30)=%.1f%%",
                int(df["n_cum"].median()), 100.0 * (df["n_cum"] >= 30).mean())
    return df


if __name__ == "__main__":
    main()
