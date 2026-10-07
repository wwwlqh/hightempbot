"""Bracket labels, membership and probabilities."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

import numpy as np

if TYPE_CHECKING:
    from hightempbot.calibration.model import CalibrationModel
    from hightempbot.stations import StationConfig


def bracket_label(
    station: StationConfig,
    bracket: tuple[str, float | None, float | None],
) -> str:
    """Format a bracket as a human-readable label."""
    unit = getattr(station, "unit", "C")
    suffix = "°F" if unit == "F" else "°C"
    btype, lo, hi = bracket

    if btype == "floor":
        return f"<{hi:.0f}{suffix}"
    elif btype == "ceiling":
        return f"≥{lo:.0f}{suffix}"
    else:
        return f"{lo:.0f}-{hi:.0f}{suffix}"


def _to_celsius(val: float | None, unit: str) -> float | None:
    if val is None:
        return None
    if unit == "F":
        from hightempbot.stations import fahrenheit_to_celsius
        return fahrenheit_to_celsius(val)
    return float(val)


def actual_in_bracket(
    actual: float,
    bracket_low: float | None,
    bracket_high: float | None,
) -> bool:
    """Whether ``actual`` is in ``[low, high)`` (either bound may be open).
    False when both bounds are None."""
    if bracket_low is None and bracket_high is not None:
        return actual < bracket_high
    if bracket_high is None and bracket_low is not None:
        return actual >= bracket_low
    if bracket_low is not None and bracket_high is not None:
        return bracket_low <= actual < bracket_high
    return False


def bracket_probabilities(
    model: CalibrationModel,
    ensemble_members: np.ndarray,
    brackets: list[tuple[str, float | None, float | None]],
    unit: str,
    icao: str = "",
) -> list[float]:
    """Probability of each bracket from the model's P(tmax > x).

    Bounds are continuous ``[lo, hi)`` as produced by ``parse_bracket_bounds``.
    Returns [] when it can't be trusted (NaN, or a missing end bracket with a
    total below 0.95); otherwise renormalizes.
    """
    probs = []
    for btype, lo, hi in brackets:
        lo_c = _to_celsius(lo, unit)
        hi_c = _to_celsius(hi, unit)

        if btype == "floor":
            p = 1.0 - model.predict(ensemble_members, hi_c)
        elif btype == "ceiling":
            p = model.predict(ensemble_members, lo_c)
        else:
            p_lo = model.predict(ensemble_members, lo_c)
            p_hi = model.predict(ensemble_members, hi_c)
            p = max(p_lo - p_hi, 0.0)

        probs.append(max(p, 0.0))

    if any(math.isnan(p) for p in probs):
        logger.warning(
            "Bracket probabilities contain NaN for %s — failing closed (no signal)",
            icao,
        )
        return []

    total = sum(probs)
    btypes = [b[0] for b in brackets]
    has_floor = "floor" in btypes
    has_ceiling = "ceiling" in btypes
    corners_present = has_floor and has_ceiling

    if total < 0.95:
        # With an end bracket missing, renormalizing would invent confidence.
        if not corners_present:
            logger.warning(
                "Bracket probability sum %.3f with missing corner for %s "
                "(floor=%s, ceiling=%s) — failing closed to avoid synthesised signal",
                total, icao, has_floor, has_ceiling,
            )
            return []
        logger.warning(
            "Bracket probability sum %.3f outside [0.95, 1.05] for %s — possible gap in market brackets",
            total, icao,
        )
    elif total > 1.05:
        logger.warning(
            "Bracket probability sum %.3f outside [0.95, 1.05] for %s — possible gap in market brackets",
            total, icao,
        )

    if total > 0:
        probs = [p / total for p in probs]

    return probs
