"""Tests for bracket boundary construction and probability computation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from unittest.mock import MagicMock

import numpy as np
import pytest

from hightempbot.decision.brackets import (
    bracket_label,
    bracket_probabilities,
    build_brackets,
)


@dataclass
class MockStation:
    icao: str
    unit: str = "C"


class TestBuildBrackets:
    def test_f_station_11_brackets(self):
        station = MockStation("KDAL", "F")
        brackets = build_brackets(station, 20.0)  # 20°C ≈ 68°F
        assert len(brackets) == 11
        assert brackets[0][0] == "floor"
        assert brackets[-1][0] == "ceiling"
        for b in brackets[1:-1]:
            assert b[0] == "interior"

    def test_f_station_2f_wide(self):
        station = MockStation("KDAL", "F")
        brackets = build_brackets(station, 20.0)
        # Interior brackets should be 2°F wide
        for b in brackets[1:-1]:
            assert b[2] - b[1] == 2.0

    def test_c_station_1c_wide(self):
        station = MockStation("RJTT", "C")
        brackets = build_brackets(station, 25.0)
        assert len(brackets) == 11
        for b in brackets[1:-1]:
            assert b[2] - b[1] == 1.0

    def test_c_unit_uses_round(self):
        station = MockStation("RJTT", "C")
        # 25.7°C → round = 26, center = 26
        brackets = build_brackets(station, 25.7)
        interior_lows = [b[1] for b in brackets if b[0] == "interior"]
        assert 26.0 in interior_lows or 25.0 in interior_lows


class TestBracketLabel:
    def test_floor_label(self):
        station = MockStation("KDAL", "F")
        label = bracket_label(station, ("floor", None, 60.0))
        assert label == "<60°F"

    def test_ceiling_label(self):
        station = MockStation("KDAL", "F")
        label = bracket_label(station, ("ceiling", 78.0, None))
        assert label == "≥78°F"

    def test_interior_label_c(self):
        station = MockStation("RJTT", "C")
        label = bracket_label(station, ("interior", 25.0, 26.0))
        assert label == "25-26°C"


class TestBracketProbabilities:
    def test_probabilities_sum_to_one(self):
        station = MockStation("KDAL", "F")
        brackets = build_brackets(station, 20.0)
        model = MagicMock()
        # Model returns decreasing P(tmax > threshold) as threshold increases
        model.predict = lambda ens, t: max(0.0, min(1.0, 0.95 - t * 0.03))
        ensemble = np.array([18.0, 20.0, 22.0])

        probs = bracket_probabilities(model, ensemble, brackets, "F")
        assert len(probs) == 11
        assert abs(sum(probs) - 1.0) < 0.001

    def test_concentrated_ensemble(self):
        station = MockStation("RJTT", "C")
        brackets = build_brackets(station, 25.0)
        model = MagicMock()
        # Very sharp CDF centered at 25°C
        model.predict = lambda ens, t: 1.0 if t < 24.5 else (0.0 if t > 25.5 else 0.5)
        ensemble = np.array([25.0, 25.0, 25.0])

        probs = bracket_probabilities(model, ensemble, brackets, "C")
        assert len(probs) == 11
        # Most probability should be in 1-2 brackets
        assert max(probs) > 0.3

    def test_interior_c_brackets_from_parser_nonzero(self):
        """Regression: before the continuous-range fix, 1°C interior brackets
        from parse_bracket_bounds collapsed to p=0 because lo==hi. With the
        parser emitting [X-0.5, X+0.5), every interior bracket near the
        ensemble mean must carry real probability mass."""
        from scipy.stats import norm

        # Brackets as produced post-fix by parse_bracket_bounds for non-US
        # 1°C markets: floor "<22°C", interiors 23..31, ceiling "≥32°C".
        parsed = [("floor", None, 22.5)]
        for label_val in range(23, 32):
            parsed.append(("interior", float(label_val) - 0.5, float(label_val) + 0.5))
        parsed.append(("ceiling", 31.5, None))
        assert len(parsed) == 11

        # Gaussian-ish CDF centered at 25.7°C, sigma ~1.5°C.
        mu, sigma = 25.7, 1.5
        model = MagicMock()
        model.predict = lambda ens, t: float(1.0 - norm.cdf((t - mu) / sigma))
        ensemble = np.array([mu, mu, mu])

        probs = bracket_probabilities(model, ensemble, parsed, "C")

        # Sum to 1 (within normalization tolerance)
        assert abs(sum(probs) - 1.0) < 0.001
        # All 9 interior brackets must carry some mass — none should be zero
        interior_probs = probs[1:10]
        assert all(p > 0.0 for p in interior_probs)
        # Peak should live near label "26" (bracket index 3) or "25" (index 2)
        peak_idx = probs.index(max(probs))
        assert 2 <= peak_idx <= 4, f"peak at idx={peak_idx} (probs={probs})"


