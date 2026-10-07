import pytest
import pandas as pd

from backtest.scripts.measure_tp_sl import (
    _tail_delayed_entry_threshold,
    apply_tail_delayed_entry,
)
from backtest.scripts.sweep_l2_depth_features import build_bets_for_config


def _tail_bet(row_idx: int = 0, entry_ts: int = 1000) -> tuple:
    return (
        "2026-06-01",
        row_idx,
        "YES",
        0.025,
        0.18,
        100.0,
        0.01,
        True,
        "TAIL",
        entry_ts,
        "extra",
    )


def _no_bet(row_idx: int = 1, entry_ts: int = 1005) -> tuple:
    return (
        "2026-06-01",
        row_idx,
        "NO",
        0.80,
        0.12,
        100.0,
        0.01,
        True,
        "NO",
        entry_ts,
    )


def test_tail_delayed_entry_rewrites_to_first_threshold_hit() -> None:
    records = [
        {"market_slug": "tail-slug", "close_ts_unix": 20000},
        {"market_slug": "no-slug", "close_ts_unix": 20000},
    ]
    prices_by_ss = {
        ("tail-slug", "Yes"): [
            (900, 0.010),
            (1100, 0.021),
            (1200, 0.019),
            (1300, 0.018),
        ],
    }
    metrics_idx = {
        "tail-slug": ([1000, 1200], [(10.0, 111.0, 0.03), (20.0, 222.0, 0.04)]),
    }

    out, stats = apply_tail_delayed_entry(
        bets=[_tail_bet(), _no_bet()],
        records=records,
        prices_by_ss=prices_by_ss,
        metrics_idx=metrics_idx,
        threshold=0.02,
    )

    assert stats == {
        "tail_signals": 1,
        "tail_entered": 1,
        "tail_missed": 0,
        "avg_delay_minutes": pytest.approx(200 / 60, abs=1e-4),
        "max_delay_hours": pytest.approx(200 / 3600, abs=1e-4),
    }
    assert _no_bet() in out

    tail = next(bet for bet in out if bet[8] == "TAIL")
    assert tail[:10] == (
        "2026-06-01",
        0,
        "YES",
        0.019,
        0.18,
        222.0,
        0.04,
        True,
        "TAIL",
        1200,
    )
    assert tail[10:] == ("extra",)


def test_tail_delayed_entry_drops_misses_after_close_cutoff() -> None:
    out, stats = apply_tail_delayed_entry(
        bets=[_tail_bet(entry_ts=1000)],
        records=[{"market_slug": "tail-slug", "close_ts_unix": 20000}],
        prices_by_ss={("tail-slug", "Yes"): [(5601, 0.019)]},
        metrics_idx={},
        threshold=0.02,
    )

    assert out == []
    assert stats["tail_signals"] == 1
    assert stats["tail_entered"] == 0
    assert stats["tail_missed"] == 1


def test_tail_delayed_entry_threshold_reads_canonical_config_field() -> None:
    assert _tail_delayed_entry_threshold({"TAIL": {"delayed_entry_fp_max": "0.02"}}) == 0.02
    assert _tail_delayed_entry_threshold({"TAIL": {"delayed_entry_fp_max": None}}) is None
    assert _tail_delayed_entry_threshold({"TAIL": {}}) is None


def test_configured_backtest_builder_applies_tail_delayed_entry() -> None:
    config = {
        "min_bvol": 50,
        "NO": {"enabled": False},
        "YMID": {"enabled": False},
        "TAIL": {
            "enabled": True,
            "vote_signals": ["p_E", "p_B_50", "p_L_loose", "p_Shrink_n10"],
            "alpha": 4.0,
            "n_required": 4,
            "fp_min": 0.001,
            "fp_max": 0.03,
            "delayed_entry_fp_max": 0.02,
            "entry_local_hours": [1],
            "size_frac": 0.05,
            "execution_min_edge": 0.07,
        },
        "YHIGH": {"enabled": False},
    }
    df = pd.DataFrame({
        "station_id": ["KAAA"],
        "market_date": ["2026-06-01"],
        "bracket_index": [0],
        "bracket_kind": ["low"],
        "won_yes": [1],
        "n_cum": [30],
        "p_E": [0.11],
        "p_B_50": [0.11],
        "p_L_loose": [0.11],
        "p_Shrink_n10": [0.11],
        "yes_price_h1": [0.025],
        "no_price_h1": [0.975],
        "entry_ts_h1": [1_800_000_000],
        "entry_volume_h1": [100.0],
        "entry_liquidity_h1": [111.0],
        "entry_spread_h1": [0.01],
    })

    bets, stats = build_bets_for_config(
        df=df,
        config=config,
        records=[{"market_slug": "tail-slug", "close_ts_unix": 1_800_100_000}],
        prices_by_ss={("tail-slug", "Yes"): [(1_800_000_300, 0.021), (1_800_000_600, 0.019)]},
        metrics_idx={"tail-slug": ([1_800_000_600], [(20.0, 222.0, 0.04)])},
    )

    assert stats["tail_delayed_entry_threshold"] == 0.02
    assert stats["tail_delayed_entry"]["tail_entered"] == 1
    assert len(bets) == 1
    assert bets[0][3] == 0.019
    assert bets[0][5] == 222.0
    assert bets[0][6] == 0.04
    assert bets[0][9] == 1_800_000_600
