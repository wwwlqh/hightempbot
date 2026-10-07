"""Parity guard: live `_compute_signal_flavors` vs `backtest/sweep_lib.add_signal_flavors`.

Ensures the live decision path produces identical p_model_<flavor> values to
the backtest harness. A drift here invalidates the backtest results as a
paper-trade comparison anchor.

Tolerance: bit-for-bit (np.allclose with rtol=0, atol=1e-12). Anything looser
risks accumulating numerical drift that compounds across the 9 flavors.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Make the project's `backtest/` package importable without install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backtest.lib.sweep_lib import add_signal_flavors  # noqa: E402
from hightempbot.decision.strategies import _compute_signal_flavors  # noqa: E402

_FLAVORS = (
    "p_E", "p_L_strict", "p_L_loose",
    "p_B_50", "p_B_30", "p_B_70",
    "p_Shrink_n10", "p_Shrink_n50", "p_Ramp",
)


@pytest.mark.parametrize(
    "p_emos, n, hits",
    [
        # Cold start: no history.
        (0.20, 0, 0),
        (0.55, 0, 0),
        # Below confidence threshold (n < lut_min_n=30).
        (0.45, 25, 12),
        (0.10, 5, 1),
        # Exactly at threshold: confident kicks in.
        (0.30, 30, 9),
        # High n.
        (0.30, 100, 35),
        (0.50, 200, 100),
        # High edge, high observed.
        (0.80, 60, 50),
        # Adversarial: hits = n (perfect bucket history).
        (0.40, 50, 50),
        # Adversarial: hits = 0 (bucket never hit).
        (0.40, 50, 0),
    ],
)
def test_parity_with_backtest_add_signal_flavors(p_emos: float, n: int, hits: int) -> None:
    live = _compute_signal_flavors(p_emos, n, hits, lut_min_n=30)
    df = pd.DataFrame([{"p_raw": p_emos, "n_cum": n, "hits_cum": hits}])
    sl = add_signal_flavors(df, lut_min_n=30).iloc[0]

    for flavor in _FLAVORS:
        live_v = float(live[flavor])
        sl_v = float(sl[flavor])
        if math.isnan(live_v) or math.isnan(sl_v):
            assert math.isnan(live_v) and math.isnan(sl_v), (
                f"NaN mismatch on {flavor}: live={live_v}, backtest={sl_v}"
            )
        else:
            assert abs(live_v - sl_v) < 1e-12, (
                f"{flavor}: live={live_v}, backtest={sl_v}, diff={live_v - sl_v}"
            )


def test_p_E_equals_input() -> None:
    flavors = _compute_signal_flavors(0.7, 10, 5)
    assert flavors["p_E"] == 0.7


def test_l_strict_nan_below_threshold() -> None:
    flavors = _compute_signal_flavors(0.3, 25, 8, lut_min_n=30)
    assert math.isnan(flavors["p_L_strict"])


def test_l_loose_falls_back_to_E_below_threshold() -> None:
    flavors = _compute_signal_flavors(0.3, 25, 8, lut_min_n=30)
    assert flavors["p_L_loose"] == 0.3
    # F-005 consequence: p_B_50 collapses to E too at small n.
    assert flavors["p_B_50"] == 0.3


def test_shrinkage_active_at_n_zero() -> None:
    # With n=0, shrinkage formula returns E exactly: (0 + 50E) / (0 + 50) = E.
    flavors = _compute_signal_flavors(0.4, 0, 0)
    assert flavors["p_Shrink_n50"] == 0.4
    assert flavors["p_Shrink_n10"] == 0.4
