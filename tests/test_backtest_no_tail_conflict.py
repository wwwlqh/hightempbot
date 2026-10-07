import pandas as pd
import pytest

from backtest.lib.live_match_eval import apply_no_tail_conflict_policy


def _bet(row_idx: int, strat: str) -> tuple:
    side = "YES" if strat == "TAIL" else "NO"
    return ("2026-05-01", row_idx, side, 0.5, 0.5, 100.0, 0.01, True, strat, 1)


def _df() -> pd.DataFrame:
    return pd.DataFrame({
        "station_id": ["KAAA", "KAAA", "KAAA", "KBBB"],
        "market_date": ["2026-05-01", "2026-05-01", "2026-05-01", "2026-05-01"],
        "bracket_index": [1, 2, 3, 1],
    })


def test_same_bracket_policy_only_resolves_exact_bracket_conflicts() -> None:
    bets = [
        _bet(0, "NO"),
        _bet(0, "TAIL"),
        _bet(1, "NO"),
        _bet(2, "TAIL"),
    ]

    filtered, stats = apply_no_tail_conflict_policy(
        bets,
        _df(),
        policy="prefer_tail",
        scope="same_bracket",
    )

    assert [bet[8] for bet in filtered] == ["TAIL", "NO", "TAIL"]
    assert stats["conflict_groups"] == 1
    assert stats["dropped_no_bets"] == 1
    assert stats["dropped_tail_bets"] == 0


def test_station_date_policy_resolves_all_no_tail_pairs_for_station_day() -> None:
    bets = [
        _bet(0, "NO"),
        _bet(0, "TAIL"),
        _bet(1, "NO"),
        _bet(2, "TAIL"),
        _bet(3, "NO"),
    ]

    filtered, stats = apply_no_tail_conflict_policy(
        bets,
        _df(),
        policy="prefer_no",
        scope="station_date",
    )

    assert [bet[8] for bet in filtered] == ["NO", "NO", "NO"]
    assert stats["conflict_groups"] == 1
    assert stats["conflict_no_bets"] == 2
    assert stats["conflict_tail_bets"] == 2
    assert stats["dropped_tail_bets"] == 2


def test_drop_both_keeps_non_no_tail_strategies_in_conflict_group() -> None:
    ymid = ("2026-05-01", 0, "YES", 0.5, 0.5, 100.0, 0.01, True, "YMID", 1)
    bets = [_bet(0, "NO"), ymid, _bet(0, "TAIL")]

    filtered, stats = apply_no_tail_conflict_policy(
        bets,
        _df(),
        policy="drop_both",
        scope="same_bracket",
    )

    assert filtered == [ymid]
    assert stats["dropped_bets"] == 2


def test_invalid_conflict_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown NO/TAIL conflict policy"):
        apply_no_tail_conflict_policy([], _df(), policy="tail please")
