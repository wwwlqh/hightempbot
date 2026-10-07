"""Tests for the WU forecast consensus gate.

Exercises bracket math, °F→°C conversion, tail brackets, and the fail-closed
handling when WU is unavailable. Buffer = 1.0°C (production default): for
1°C-wide interior brackets, YES candidates always reject (no point in the
interior is 1°C clear of both edges); NO candidates need ≥1°C clearance from
either edge. Tail brackets retain enough room for both sides.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hightempbot.decision.strategies import _gate_wu_consensus


def _gate(
    *,
    bracket_low: float | None,
    bracket_high: float | None,
    unit: str,
    side: str,
    wu_max_c: float | None,
    buffer_c: float = 1.0,
):
    """Helper that mocks ``fetch_wu_forecast`` and runs the gate."""
    with patch(
        "hightempbot.decision.strategies.fetch_wu_forecast",
        return_value=wu_max_c,
    ):
        return _gate_wu_consensus(
            bracket_low,
            bracket_high,
            unit,
            side,
            station_id="KLAX",
            target_date="2026-05-04",
            buffer_c=buffer_c,
        )


# ---- Celsius interior bracket: "26°C" → [25.5, 26.5), buffer 1°C ----

def test_yes_interior_always_rejects():
    # With buffer=1.0 the gate has lo_eff=26.5, hi_eff=25.5 → impossible to be
    # "clearly_in" any 1°C-wide interior bracket. All YES interior bets reject.
    for wu in (25.6, 26.0, 26.4):
        passed, _ = _gate(bracket_low=25.5, bracket_high=26.5, unit="C", side="YES", wu_max_c=wu)
        assert passed is False, f"expected reject for wu={wu}"


def test_no_clearly_below_accepts():
    # NO needs wu < lo - buffer = 25.5 - 1.0 = 24.5.
    passed, _ = _gate(bracket_low=25.5, bracket_high=26.5, unit="C", side="NO", wu_max_c=24.0)
    assert passed is True


def test_no_clearly_above_accepts():
    # NO needs wu >= hi + buffer = 26.5 + 1.0 = 27.5.
    passed, _ = _gate(bracket_low=25.5, bracket_high=26.5, unit="C", side="NO", wu_max_c=28.0)
    assert passed is True


def test_no_inside_bracket_rejects():
    passed, _ = _gate(bracket_low=25.5, bracket_high=26.5, unit="C", side="NO", wu_max_c=26.0)
    assert passed is False


def test_no_within_lower_buffer_rejects():
    # 24.6 is below lo=25.5 but only 0.9°C below — within the 1.0 buffer.
    passed, _ = _gate(bracket_low=25.5, bracket_high=26.5, unit="C", side="NO", wu_max_c=24.6)
    assert passed is False


def test_no_within_upper_buffer_rejects():
    # 27.4 is above hi=26.5 but only 0.9°C above — within the 1.0 buffer.
    passed, _ = _gate(bracket_low=25.5, bracket_high=26.5, unit="C", side="NO", wu_max_c=27.4)
    assert passed is False


# ---- Fahrenheit interior bracket: "60-61°F" → [59.5, 61.5)°F = [15.28, 16.39)°C ----

def test_yes_fahrenheit_interior_always_rejects():
    # F bracket width ≈ 1.11°C, barely wider than 1°C buffer → still impossible
    # for YES to clear both edges by 1°C.
    for wu in (15.5, 15.83, 16.2):
        passed, _ = _gate(bracket_low=59.5, bracket_high=61.5, unit="F", side="YES", wu_max_c=wu)
        assert passed is False, f"expected reject for wu={wu}"


def test_no_fahrenheit_clearly_below():
    # lo_c = (59.5-32)*5/9 ≈ 15.28; need wu < 15.28 - 1 = 14.28
    passed, _ = _gate(bracket_low=59.5, bracket_high=61.5, unit="F", side="NO", wu_max_c=14.0)
    assert passed is True


def test_no_fahrenheit_clearly_above():
    # hi_c ≈ 16.39; need wu >= 16.39 + 1 = 17.39
    passed, _ = _gate(bracket_low=59.5, bracket_high=61.5, unit="F", side="NO", wu_max_c=18.0)
    assert passed is True


def test_no_fahrenheit_inside_rejects():
    passed, _ = _gate(bracket_low=59.5, bracket_high=61.5, unit="F", side="NO", wu_max_c=15.83)
    assert passed is False


# ---- Tail brackets ----

def test_yes_floor_tail_accept():
    # "<34°C" tail → bracket_high=33.5; YES needs wu < 33.5 - 1 = 32.5.
    passed, _ = _gate(bracket_low=None, bracket_high=33.5, unit="C", side="YES", wu_max_c=30.0)
    assert passed is True


def test_yes_floor_tail_within_buffer_rejects():
    # "<34°C" tail with WU = 32.7 → above 32.5 threshold, in buffer → reject.
    passed, _ = _gate(bracket_low=None, bracket_high=33.5, unit="C", side="YES", wu_max_c=32.7)
    assert passed is False


def test_no_floor_tail_clearly_above():
    # NO on "<34°C" needs wu >= hi + buffer = 33.5 + 1 = 34.5.
    passed, _ = _gate(bracket_low=None, bracket_high=33.5, unit="C", side="NO", wu_max_c=35.0)
    assert passed is True


def test_no_floor_tail_within_buffer_rejects():
    # NO on "<34°C" with wu=34.3 → only 0.8 above hi, within buffer → reject.
    passed, _ = _gate(bracket_low=None, bracket_high=33.5, unit="C", side="NO", wu_max_c=34.3)
    assert passed is False


def test_yes_ceiling_tail_accept():
    # "≥33°C" tail → bracket_low=32.5; YES needs wu >= 32.5 + 1 = 33.5.
    passed, _ = _gate(bracket_low=32.5, bracket_high=None, unit="C", side="YES", wu_max_c=34.0)
    assert passed is True


def test_yes_ceiling_tail_within_buffer_rejects():
    # "≥33°C" tail with wu=33.0 → above lo but only 0.5 above, within buffer → reject.
    passed, _ = _gate(bracket_low=32.5, bracket_high=None, unit="C", side="YES", wu_max_c=33.0)
    assert passed is False


def test_no_ceiling_tail_clearly_below():
    # NO on "≥33°C" needs wu < lo - buffer = 32.5 - 1 = 31.5.
    passed, _ = _gate(bracket_low=32.5, bracket_high=None, unit="C", side="NO", wu_max_c=28.0)
    assert passed is True


def test_no_ceiling_tail_within_buffer_rejects():
    # NO on "≥33°C" with wu=32.0 → only 0.5 below lo, within buffer → reject.
    passed, _ = _gate(bracket_low=32.5, bracket_high=None, unit="C", side="NO", wu_max_c=32.0)
    assert passed is False


# ---- Failure modes ----

def test_wu_unavailable_returns_none():
    passed, wu = _gate(bracket_low=25.5, bracket_high=26.5, unit="C", side="YES", wu_max_c=None)
    assert passed is None
    assert wu is None


def test_unknown_side_rejects():
    passed, wu = _gate(bracket_low=25.5, bracket_high=26.5, unit="C", side="???", wu_max_c=26.0)
    assert passed is False
    assert wu == 26.0


def test_invalid_target_date_returns_none():
    # If target_date can't be parsed we cannot fetch WU → fail closed.
    with patch("hightempbot.decision.strategies.fetch_wu_forecast", return_value=None):
        passed, wu = _gate_wu_consensus(
            25.5, 26.5, "C", "YES",
            station_id="KLAX",
            target_date="not-a-date",
        )
    assert passed is None
    assert wu is None


# ---- Boundary semantics with buffer=1.0 ----

def test_no_at_lower_minus_buffer_rejects():
    # WU = 24.5 exactly = lo - buffer = 25.5 - 1 = 24.5; comparator is `<`,
    # so 24.5 < 24.5 is False → not "out_low" → reject.
    passed, _ = _gate(bracket_low=25.5, bracket_high=26.5, unit="C", side="NO", wu_max_c=24.5)
    assert passed is False


def test_no_just_past_lower_buffer_accepts():
    # WU = 24.4 → 24.4 < 24.5 = lo - buffer → out_low → accept.
    passed, _ = _gate(bracket_low=25.5, bracket_high=26.5, unit="C", side="NO", wu_max_c=24.4)
    assert passed is True


def test_no_at_upper_plus_buffer_accepts():
    # WU = 27.5 = hi + buffer; comparator is `>=` → 27.5 >= 27.5 True → out_high → accept.
    passed, _ = _gate(bracket_low=25.5, bracket_high=26.5, unit="C", side="NO", wu_max_c=27.5)
    assert passed is True


@pytest.mark.parametrize(
    "wu, side, expected",
    [
        # Interior YES always rejects with buffer=1.0 on a 1°C-wide bracket.
        (26.0, "YES", False),
        (26.4, "YES", False),
        (25.6, "YES", False),
        # Interior NO requires ≥1°C clearance from either edge.
        (24.0, "NO", True),
        (28.0, "NO", True),
        (26.0, "NO", False),
        (24.4, "NO", True),   # just past lo - 1
        (24.6, "NO", False),  # within buffer
        (27.4, "NO", False),  # within buffer
        (27.6, "NO", True),   # past hi + 1
    ],
)
def test_parametrized_celsius(wu, side, expected):
    passed, _ = _gate(bracket_low=25.5, bracket_high=26.5, unit="C", side=side, wu_max_c=wu)
    assert passed is expected


# ---- Production buffer = 0.0: literal "in the bracket" semantics ----

@pytest.mark.parametrize(
    "wu, side, expected",
    [
        # Interior YES on bracket [25.5, 26.5):
        (25.5, "YES", True),    # exact lower edge — half-open, included
        (26.0, "YES", True),    # mid-bracket
        (26.4, "YES", True),    # just under hi
        (26.5, "YES", False),   # at hi — half-open, excluded
        (25.4, "YES", False),   # below lo
        (27.0, "YES", False),   # adjacent bracket
        # Interior NO on bracket [25.5, 26.5):
        (25.4, "NO", True),     # below lo, not in bracket
        (26.5, "NO", True),     # at hi, not in bracket
        (27.0, "NO", True),     # well outside
        (25.5, "NO", False),    # at lo, in bracket
        (26.0, "NO", False),    # mid-bracket
        (26.4, "NO", False),    # just under hi, in bracket
    ],
)
def test_buffer_zero_literal_in_bracket(wu, side, expected):
    """With buffer=0 the gate is the exact half-open membership check."""
    passed, _ = _gate(
        bracket_low=25.5, bracket_high=26.5, unit="C", side=side,
        wu_max_c=wu, buffer_c=0.0,
    )
    assert passed is expected


def test_buffer_zero_tail_ceiling_yes():
    # "≥33°C" = [32.5, +inf). With buffer=0, YES accepts at exactly 32.5.
    passed, _ = _gate(
        bracket_low=32.5, bracket_high=None, unit="C", side="YES",
        wu_max_c=32.5, buffer_c=0.0,
    )
    assert passed is True


def test_buffer_zero_tail_floor_yes():
    # "<34°C" = (-inf, 33.5). With buffer=0, YES accepts when WU < 33.5 strictly.
    passed, _ = _gate(
        bracket_low=None, bracket_high=33.5, unit="C", side="YES",
        wu_max_c=33.4, buffer_c=0.0,
    )
    assert passed is True
    passed, _ = _gate(
        bracket_low=None, bracket_high=33.5, unit="C", side="YES",
        wu_max_c=33.5, buffer_c=0.0,
    )
    assert passed is False  # at hi exclusively excluded
