from __future__ import annotations

import numpy as np

from backtest.scripts.lut_range_chunks import (
    Chunk,
    date_chunks,
    filter_bets_by_lut,
    in_range,
    probability_ranges,
)


def test_probability_ranges_includes_contiguous_windows():
    ranges = probability_ranges(0.02)

    assert (0.02, 0.04) in ranges
    assert (0.4, 0.6) in ranges
    assert (0.0, 1.0) in ranges


def test_date_chunks_splits_chronological_quarters():
    chunks = date_chunks(
        [
            "2026-01-04",
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
            "2026-01-05",
        ],
        n_chunks=4,
    )

    assert chunks == [
        Chunk("A", "2026-01-01", "2026-01-02"),
        Chunk("B", "2026-01-03", "2026-01-03"),
        Chunk("C", "2026-01-04", "2026-01-04"),
        Chunk("D", "2026-01-05", "2026-01-05"),
    ]


def test_in_range_is_half_open_except_one():
    values = np.array([0.02, 0.0399, 0.04, 1.0])

    assert in_range(values, 0.02, 0.04).tolist() == [True, True, False, False]
    assert in_range(values, 0.04, 1.0).tolist() == [False, False, True, True]


def test_filter_bets_can_use_yes_lut_or_bet_side_probability():
    bets = [
        ("2026-01-01", 0, "YES"),
        ("2026-01-01", 1, "NO"),
    ]
    lut_yes = np.array([0.03, 0.97])

    assert filter_bets_by_lut(bets, lut_yes, 0.02, 0.04, "yes") == [bets[0]]
    assert filter_bets_by_lut(bets, lut_yes, 0.02, 0.04, "bet") == bets
