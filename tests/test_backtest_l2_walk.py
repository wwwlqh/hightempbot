from __future__ import annotations

from backtest.lib.live_match_eval import ask_ladder_for_record, bid_ladder_for_record, simulate_walk_book


def test_simulate_walk_book_uses_real_l2_ladder_when_available():
    walked = simulate_walk_book(
        target_usd=10.0,
        displayed_price=0.50,
        liquidity=1.0,
        signal_p=0.80,
        theta=0.05,
        min_edge=0.03,
        min_bet_usd=1.0,
        entry_spread=0.50,
        ask_ladder="[[0.60, 10], [0.61, 10]]",
    )

    assert walked is not None
    stake, vwap, edge = walked
    assert stake == 10.0
    assert round(vwap, 4) == 0.6040
    assert edge > 0.03


def test_simulate_walk_book_trims_real_l2_ladder_at_edge_floor():
    walked = simulate_walk_book(
        target_usd=10.0,
        displayed_price=0.50,
        liquidity=1000.0,
        signal_p=0.70,
        theta=0.05,
        min_edge=0.03,
        min_bet_usd=1.0,
        ask_ladder=[[0.60, 10], [0.90, 100]],
    )

    assert walked is not None
    stake, vwap, edge = walked
    assert round(stake, 2) == 6.0
    assert round(vwap, 4) == 0.60
    assert edge > 0.03


def test_ask_ladder_for_record_matches_entry_hour_and_side():
    record = {
        "entry_ts_h0": 100.0,
        "entry_ts_h1": 200.0,
        "yes_ask_ladder_h1": "[[0.20, 5]]",
        "no_ask_ladder_h1": "[[0.80, 7]]",
    }

    assert ask_ladder_for_record(record, "YES", 200) == [(0.20, 5.0)]
    assert ask_ladder_for_record(record, "NO", 200) == [(0.80, 7.0)]
    assert ask_ladder_for_record(record, "YES", 100) is None


def test_bid_ladder_for_record_matches_entry_hour_and_side():
    record = {
        "entry_ts_h0": 100.0,
        "entry_ts_h1": 200.0,
        "yes_bid_ladder_h1": "[[0.18, 5]]",
        "no_bid_ladder_h1": "[[0.78, 7]]",
    }

    assert bid_ladder_for_record(record, "YES", 200) == [(0.18, 5.0)]
    assert bid_ladder_for_record(record, "NO", 200) == [(0.78, 7.0)]
    assert bid_ladder_for_record(record, "YES", 100) is None
