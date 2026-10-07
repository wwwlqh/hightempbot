"""Bracket boundary construction and probability vector.

F-stations use 2°F wide brackets, C-stations use 1°C.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

import numpy as np

if TYPE_CHECKING:
    from hightempbot.calibration.model import CalibrationModel
    from hightempbot.stations import StationConfig


def build_brackets(
    station: StationConfig,
    ensemble_median_c: float,
) -> list[tuple[str, float | None, float | None]]:
    """Build 11 bracket boundaries for a station based on ensemble median.

    Returns list of (bracket_type, lower_bound, upper_bound) in display units.
    bracket_type is "floor", "interior", or "ceiling".
    Bounds are in °F for US stations, °C for others.
    """
    unit = getattr(station, "unit", "C")

    if unit == "F":
        from hightempbot.stations import celsius_to_fahrenheit
        center = round(celsius_to_fahrenheit(ensemble_median_c))
        low = center - 9
        if low % 2 != 0:
            low -= 1
        brackets = [("floor", None, float(low))]
        for i in range(9):
            lo = low + i * 2
            brackets.append(("interior", float(lo), float(lo + 2)))
        brackets.append(("ceiling", float(low + 18), None))
    else:
        center = round(ensemble_median_c)
        low = center - 4
        brackets = [("floor", None, float(low))]
        for i in range(9):
            brackets.append(("interior", float(low + i), float(low + i + 1)))
        brackets.append(("ceiling", float(low + 9), None))

    return brackets


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
    """Return whether ``actual`` falls inside the half-open bracket ``[lo, hi)``.

    Single source of truth for the bracket-membership rule used everywhere
    a Polymarket bracket is compared against an observed temperature:
    floor brackets are ``actual < high``, ceiling brackets are
    ``actual >= low``, interiors are ``low <= actual < high``. Both bounds
    None means an unbounded bracket — never a real Polymarket case — so we
    return False rather than silently matching everything.
    """
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
    """Compute calibrated probability for each bracket.

    Uses CalibrationModel.predict(ensemble, threshold) which returns P(tmax > threshold).

    Bracket bounds are the TRUE continuous [lo, hi) actual-temperature range
    (produced by _parse_bracket_bounds under ROUND semantics). No further shift needed.
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
        # Fail closed: NaN propagation through any bracket means we cannot
        # trust ANY entry. Returning [0.0]*N would silently make every NO
        # bet look free-money. Empty list signals the caller to skip.
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
        # Two cases:
        #   - Both corners present and sum still < 0.95 → wide ensemble or
        #     numeric drift; renormalising is safe (we are correcting a
        #     small rounding error, not synthesising signal).
        #   - A corner is missing AND sum collapsed → renormalising would
        #     fabricate confidence in the visible brackets. Fail closed.
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
