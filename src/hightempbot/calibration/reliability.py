"""Reliability calibration layer for the live NO gate.

Live forensics (2026-07-16) showed the NO gate's *claimed* probability
(``P(NO wins) = 1 - p_E``) is systematically over-confident versus the
*realized* win rate:

    overall   claimed 0.945  vs  realized 0.881   (+6.4pp over-confident)
    °C bracs  +10.4pp over-confident   (ROS -2.31%)
    °F bracs  +1.3pp  over-confident   (ROS +8.07%)

This module fits a monotone reliability curve (pool-adjacent-violators
isotonic regression) that maps a *claimed* probability to a *calibrated*
probability, fit **separately per bracket-unit group** (``NO_C`` vs
``NO_F``). The calibrated value is substituted for the raw claimed value
*before* the edge/fee computation in the NO gate so the edge band gates on
a reliability-corrected number.

Design constraints:
  * Dependency-light — pure-python PAV, no numpy/scipy needed (numpy is on
    the server but this module deliberately avoids it so it can run anywhere).
  * Curves persist in SQLite (``reliability_curves`` table) and the loader
    picks the latest ``is_active`` curve per ``group_key``.
  * Guardrails: a loaded curve is REFUSED (falls back to identity, logs via
    ``pipeline_health``) when it was fit on ``< MIN_PAIRS`` pairs or is older
    than ``MAX_AGE_DAYS`` — a stale/thin curve is worse than no curve.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# --- Guardrails (module-level so tests + CLI can import the exact thresholds) ---
# A curve fit on fewer than this many (claimed, won) pairs is statistically
# too noisy to trust; fall back to identity rather than distort the edge band.
MIN_PAIRS: int = 200
# A curve older than this many days may no longer reflect current market /
# model behavior; fall back to identity and let monitoring flag the staleness.
MAX_AGE_DAYS: int = 45

# Group keys. The NO gate is the only calibrated path today; the ``NO_`` prefix
# leaves room for future YES-side groups without a schema change.
GROUP_NO_C = "NO_C"
GROUP_NO_F = "NO_F"
NO_GROUPS = (GROUP_NO_C, GROUP_NO_F)

# --- Curve types (persisted in the versioned ``curve_json`` payload) -----------
# Legacy isotonic payloads carry no ``type`` key; the loader treats a missing
# ``type`` as isotonic so already-saved curves keep loading unchanged.
CURVE_TYPE_ISOTONIC = "isotonic"
CURVE_TYPE_BLEND = "logit_blend"

# A market-aware logit blend is refused (identity fallback) when the fit puts
# more than this fraction of its slope weight on the market price. Past this
# point ``calibrated`` tracks the market so tightly that
# ``edge = calibrated - price - fee`` collapses to ~0 by construction and the
# gate stops firing — a result that must be surfaced to the operator, never
# silently shipped. See ``blend_degenerate_reason``.
BLEND_MAX_PRICE_WEIGHT = 0.90

# logit/sigmoid clamp — keeps ``logit(0)`` / ``logit(1)`` finite so a boundary
# probability (or price) never produces ``inf`` in the blend inference.
_LOGIT_EPS = 1e-6


def group_for_unit(bracket_unit: str, *, side: str = "NO") -> str | None:
    """Return the reliability group key for ``bracket_unit`` on ``side``.

    ``"F" -> "NO_F"``, ``"C" -> "NO_C"``. Any other unit (including the empty
    sentinel) returns ``None`` — the caller must treat that as "no calibration",
    never guess a group. Only the NO side is calibrated today.
    """
    if side != "NO":
        return None
    if bracket_unit == "F":
        return GROUP_NO_F
    if bracket_unit == "C":
        return GROUP_NO_C
    return None


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _logit(p: float) -> float:
    """Clamped log-odds. Boundary probabilities are pulled to ``_LOGIT_EPS`` so
    the result is always finite (``logit(0)`` / ``logit(1)`` would be ±inf)."""
    q = min(1.0 - _LOGIT_EPS, max(_LOGIT_EPS, float(p)))
    return math.log(q / (1.0 - q))


def _sigmoid(x: float) -> float:
    """Numerically-stable logistic sigmoid (no overflow for large |x|)."""
    if x >= 0.0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


# ----------------------------------------------------------------------------
# Pool-adjacent-violators (PAV) isotonic regression
# ----------------------------------------------------------------------------

def fit_isotonic(
    pairs: list[tuple[float, float]],
) -> tuple[list[float], list[float]]:
    """Fit a monotone non-decreasing isotonic curve via PAV.

    ``pairs`` is a list of ``(claimed, won)`` where ``claimed`` is a
    probability in [0,1] and ``won`` is 0 or 1 (the realized outcome). Returns
    ``(breakpoints, values)`` — two equal-length lists describing a piecewise
    monotone curve suitable for linear interpolation:

      * ``breakpoints`` is strictly increasing in ``claimed``.
      * ``values`` is non-decreasing (the isotonic constraint) and each entry
        is the pooled empirical win-rate for that breakpoint.

    Ties in ``claimed`` are pooled first (a function must be single-valued at a
    given x), then adjacent blocks that violate monotonicity are merged with a
    weighted mean until the whole sequence is non-decreasing. Flat blocks are
    emitted as their two endpoints so the returned curve is compact.
    """
    if not pairs:
        return [], []
    pts = sorted(((float(c), float(w)) for c, w in pairs), key=lambda t: t[0])

    # Group identical-x points into a single block first (single-valued at x).
    # block = [x_lo, x_hi, sum_y, weight]
    blocks: list[list[float]] = []
    for x, y in pts:
        if blocks and blocks[-1][0] == x and blocks[-1][1] == x:
            blocks[-1][2] += y
            blocks[-1][3] += 1
        else:
            blocks.append([x, x, y, 1.0])

    # PAV: push each block; while the last block's mean is below the previous
    # block's mean, merge (weighted). Guarantees a non-decreasing sequence.
    merged: list[list[float]] = []
    for b in blocks:
        merged.append(b)
        while len(merged) >= 2 and (
            merged[-1][2] / merged[-1][3] < merged[-2][2] / merged[-2][3]
        ):
            last = merged.pop()
            prev = merged.pop()
            merged.append([
                prev[0],            # x_lo (earliest)
                last[1],            # x_hi (latest)
                prev[2] + last[2],  # sum_y
                prev[3] + last[3],  # weight
            ])

    breakpoints: list[float] = []
    values: list[float] = []
    for x_lo, x_hi, sum_y, weight in merged:
        v = sum_y / weight
        breakpoints.append(x_lo)
        values.append(v)
        if x_hi != x_lo:
            breakpoints.append(x_hi)
            values.append(v)
    return breakpoints, values


# ----------------------------------------------------------------------------
# Market-aware logit blend
# ----------------------------------------------------------------------------

def fit_logit_blend(
    triples: list[tuple[float, float, float]],
    *,
    l2: float = 1e-4,
    max_iter: int = 100,
    tol: float = 1e-9,
) -> list[float]:
    """Fit ``P(won) = sigmoid(b0 + b1·logit(claimed) + b2·logit(no_price))``.

    ``triples`` is a list of ``(claimed, no_price, won)`` where ``claimed`` is
    the model's ``P(NO wins)``, ``no_price`` is the market NO price at entry, and
    ``won`` is the realized 0/1 outcome. Returns ``[b0, b1, b2]``.

    Why a market-aware blend: a 1D claimed→realized isotonic curve cannot close
    the NO gate's over-confidence because it is *selection-conditional* — over
    the full candidate population claimed ≈ realized, but on the gate-firing
    slice (model disagrees with market) the market carries information the model
    lacks. Adding ``logit(no_price)`` as a second feature lets the calibrator
    pull the model's claim toward the market when the two disagree.

    Fit is deterministic IRLS / Newton–Raphson from a zero start. numpy is
    imported lazily *inside* this offline fitter so the module (and the live
    inference path :meth:`ReliabilityCurve.apply`, which is pure-python) never
    hard-depends on numpy. A tiny L2 ridge on the two slopes (never the
    intercept) keeps the 3×3 solve well-conditioned when claimed and price are
    near-collinear; at the ~thousands-of-rows scale used here it does not
    meaningfully bias the coefficients.

    Returns ``[]`` (caller treats as "no blend" / identity) when there are fewer
    than 3 usable triples, when every label is identical (no slope is
    identifiable), or when the solve fails to produce finite coefficients.
    """
    import numpy as np  # lazy: keeps live inference numpy-free

    rows: list[tuple[float, float, float]] = []
    for c, pr, w in triples:
        try:
            cf = float(c)
            prf = float(pr)
            wf = float(w)
        except (TypeError, ValueError):
            continue
        if cf != cf or prf != prf or wf != wf:  # NaN guard
            continue
        rows.append((_logit(cf), _logit(prf), 1.0 if wf >= 0.5 else 0.0))
    if len(rows) < 3:
        return []

    X = np.array([[1.0, a, b] for a, b, _ in rows], dtype=float)
    y = np.array([r[2] for r in rows], dtype=float)
    if float(y.min()) == float(y.max()):
        # Degenerate label distribution — no slope is identifiable.
        return []

    beta = np.zeros(3, dtype=float)
    ridge = np.diag([0.0, l2, l2])
    for _ in range(max_iter):
        eta = X @ beta
        p = 1.0 / (1.0 + np.exp(-eta))
        p = np.clip(p, 1e-9, 1.0 - 1e-9)
        w_irls = p * (1.0 - p)
        grad = X.T @ (y - p) - np.array([0.0, l2 * beta[1], l2 * beta[2]])
        hess = X.T @ (X * w_irls[:, None]) + ridge
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        beta = beta + step
        if float(np.max(np.abs(step))) < tol:
            break

    if not bool(np.all(np.isfinite(beta))):
        return []
    return [float(b) for b in beta]


def blend_degenerate_reason(coefs: list[float]) -> str | None:
    """Return why a logit-blend fit is degenerate, or ``None`` if healthy.

    A market-aware blend is trustworthy only when the model claim and the market
    price *both* push the calibrated probability up (positive slopes) and the
    model claim keeps a meaningful share of the weight. Failing that, the
    calibrated value tracks the market so tightly that
    ``edge = calibrated - price - fee`` collapses to ~0 and the gate stops firing
    by construction. That is a real finding — the raw "edge" was mostly
    miscalibration — and it must be surfaced to the operator, not silently
    shipped as a curve that quietly halts trading.

    Reasons (all fail the guardrail):
      * ``b1 <= 0`` — the model claim is not positively predictive (broken fit).
      * ``b2 < 0``  — the market price is wrong-signed (broken fit).
      * price weight ``|b2| / (|b1| + |b2|) > BLEND_MAX_PRICE_WEIGHT`` — the blend
        is essentially "trust the market", edge → 0.
    """
    if len(coefs) < 3:
        return "no coefs (identity)"
    b1, b2 = float(coefs[1]), float(coefs[2])
    if b1 <= 0.0:
        return f"b1={b1:.4f} <= 0 (model claim not positively predictive)"
    if b2 < 0.0:
        return f"b2={b2:.4f} < 0 (market price wrong-signed)"
    denom = abs(b1) + abs(b2)
    if denom <= 0.0:
        return "b1=b2=0 (identity)"
    price_weight = abs(b2) / denom
    if price_weight > BLEND_MAX_PRICE_WEIGHT:
        return (
            f"price_weight={price_weight:.3f} > {BLEND_MAX_PRICE_WEIGHT} "
            "(edge collapses to ~0; raw 'edge' was mostly miscalibration)"
        )
    return None


# ----------------------------------------------------------------------------
# Reliability curve
# ----------------------------------------------------------------------------

@dataclass
class ReliabilityCurve:
    """A fitted claimed->calibrated probability curve (isotonic or logit blend).

    Two ``curve_type`` variants share one dataclass so the persistence layer,
    guardrails, and :class:`ReliabilityProvider` treat both uniformly:

      * ``isotonic`` — a monotone claimed->realized curve (PAV, ``breakpoints`` +
        ``values``). Ignores the market price. This is the legacy variant and
        the default for any payload with no ``type`` key.
      * ``logit_blend`` — a market-aware blend
        ``sigmoid(b0 + b1·logit(claimed) + b2·logit(no_price))`` (``coefs``).
        Requires the market NO price at apply time.

    An *empty* curve (no breakpoints for isotonic, <3 coefs for a blend) is the
    identity: ``apply`` clamps its input to [0,1] and returns it unchanged. This
    is the safe fallback used everywhere a real curve is unavailable or fails a
    guardrail.
    """

    breakpoints: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    n_pairs: int = 0
    fitted_at: str = ""
    group_key: str = ""
    curve_type: str = CURVE_TYPE_ISOTONIC
    coefs: list[float] = field(default_factory=list)

    @property
    def is_identity(self) -> bool:
        if self.curve_type == CURVE_TYPE_BLEND:
            return len(self.coefs) < 3
        return not self.breakpoints

    @classmethod
    def fit(
        cls,
        pairs: list[tuple[float, float]],
        *,
        group_key: str = "",
        fitted_at: str | None = None,
    ) -> "ReliabilityCurve":
        bps, vals = fit_isotonic(pairs)
        return cls(
            breakpoints=bps,
            values=vals,
            n_pairs=len(pairs),
            fitted_at=fitted_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            group_key=group_key,
        )

    @classmethod
    def fit_blend(
        cls,
        triples: list[tuple[float, float, float]],
        *,
        group_key: str = "",
        fitted_at: str | None = None,
        l2: float = 1e-4,
    ) -> "ReliabilityCurve":
        """Fit a market-aware logit-blend curve from ``(claimed, no_price, won)``.

        See :func:`fit_logit_blend`. An empty/degenerate fit yields a curve whose
        ``coefs`` are ``[]`` — i.e. an identity curve — so callers never have to
        special-case a failed fit.
        """
        coefs = fit_logit_blend(triples, l2=l2)
        return cls(
            curve_type=CURVE_TYPE_BLEND,
            coefs=coefs,
            n_pairs=len(triples),
            fitted_at=fitted_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            group_key=group_key,
        )

    def apply(self, claimed: float, no_price: float | None = None) -> float:
        """Map ``claimed`` -> calibrated. ``no_price`` is the market NO price.

        Isotonic curves ignore ``no_price`` (the argument is accepted only to
        keep one uniform signature across curve types). Blend curves require it;
        when it is missing/NaN the blend falls back to identity rather than
        extrapolate. The result is always clamped to [0,1].
        """
        try:
            x = float(claimed)
        except (TypeError, ValueError):
            return 0.0
        if x != x:  # NaN guard
            return 0.0
        if self.curve_type == CURVE_TYPE_BLEND:
            return self._apply_blend(x, no_price)
        return self._apply_isotonic(x)

    def _apply_isotonic(self, x: float) -> float:
        """Clamped linear interpolation over the PAV breakpoints.

        Below the first breakpoint returns the first value; above the last
        returns the last value (flat extrapolation). Identity when empty.
        """
        bps = self.breakpoints
        vals = self.values
        if not bps:
            return _clamp01(x)
        if x <= bps[0]:
            return _clamp01(vals[0])
        if x >= bps[-1]:
            return _clamp01(vals[-1])
        i = bisect.bisect_right(bps, x)
        x0, x1 = bps[i - 1], bps[i]
        v0, v1 = vals[i - 1], vals[i]
        if x1 == x0:
            return _clamp01(v1)
        t = (x - x0) / (x1 - x0)
        return _clamp01(v0 + t * (v1 - v0))

    def _apply_blend(self, x: float, no_price: float | None) -> float:
        """``sigmoid(b0 + b1·logit(x) + b2·logit(no_price))``, clamped.

        Falls back to identity (clamped ``x``) when the fit is empty/degenerate
        (<3 coefs) or when no usable market price is supplied — a blend without
        its second feature cannot be evaluated, and silently dropping the price
        term would ship an unvalidated 1D curve.
        """
        if len(self.coefs) < 3:
            return _clamp01(x)
        if no_price is None:
            return _clamp01(x)
        try:
            pr = float(no_price)
        except (TypeError, ValueError):
            return _clamp01(x)
        if pr != pr:  # NaN guard
            return _clamp01(x)
        b0, b1, b2 = self.coefs[0], self.coefs[1], self.coefs[2]
        return _clamp01(_sigmoid(b0 + b1 * _logit(x) + b2 * _logit(pr)))

    def to_json(self) -> str:
        if self.curve_type == CURVE_TYPE_BLEND:
            return json.dumps({
                "type": CURVE_TYPE_BLEND,
                "coefs": self.coefs,
                "n_pairs": self.n_pairs,
                "fitted_at": self.fitted_at,
                "group_key": self.group_key,
            })
        # Isotonic payload deliberately carries no ``type`` key so it stays
        # byte-for-byte compatible with curves saved before the blend variant.
        return json.dumps({
            "breakpoints": self.breakpoints,
            "values": self.values,
            "n_pairs": self.n_pairs,
            "fitted_at": self.fitted_at,
            "group_key": self.group_key,
        })

    @classmethod
    def from_json(cls, raw: str, *, group_key: str = "", fitted_at: str = "", n_pairs: int = 0) -> "ReliabilityCurve":
        data = json.loads(raw)
        common = dict(
            n_pairs=int(data.get("n_pairs", n_pairs) or n_pairs),
            fitted_at=str(data.get("fitted_at", fitted_at) or fitted_at),
            group_key=str(data.get("group_key", group_key) or group_key),
        )
        # Missing ``type`` -> isotonic, so legacy payloads load unchanged.
        if data.get("type") == CURVE_TYPE_BLEND:
            return cls(
                curve_type=CURVE_TYPE_BLEND,
                coefs=[float(x) for x in data.get("coefs", [])],
                **common,
            )
        return cls(
            breakpoints=[float(x) for x in data.get("breakpoints", [])],
            values=[float(x) for x in data.get("values", [])],
            **common,
        )


# Shared identity singleton for hot-path fallbacks.
IDENTITY_CURVE = ReliabilityCurve()


# ----------------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS reliability_curves (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key   TEXT NOT NULL,
    fitted_at   TEXT NOT NULL,
    n_pairs     INTEGER NOT NULL,
    curve_json  TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
)
"""


def ensure_reliability_table(conn: sqlite3.Connection) -> None:
    """Idempotent schema create — safe to call from loader / CLI on any DB."""
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reliability_group_active "
        "ON reliability_curves(group_key, is_active, id DESC)"
    )


def save_curve(conn: sqlite3.Connection, curve: ReliabilityCurve) -> int:
    """Persist ``curve`` as the new active curve for its ``group_key``.

    Deactivates any previously-active curve for the same group first so the
    loader's "latest active per group" contract yields exactly one row.
    Returns the new row id.
    """
    if not curve.group_key:
        raise ValueError("save_curve: curve.group_key must be set")
    ensure_reliability_table(conn)
    conn.execute(
        "UPDATE reliability_curves SET is_active = 0 WHERE group_key = ? AND is_active = 1",
        (curve.group_key,),
    )
    cur = conn.execute(
        "INSERT INTO reliability_curves (group_key, fitted_at, n_pairs, curve_json, is_active) "
        "VALUES (?, ?, ?, ?, 1)",
        (curve.group_key, curve.fitted_at, curve.n_pairs, curve.to_json()),
    )
    conn.commit()
    return int(cur.lastrowid)


def load_active_curve(conn: sqlite3.Connection, group_key: str) -> ReliabilityCurve | None:
    """Return the latest active curve for ``group_key`` (raw, unguarded)."""
    ensure_reliability_table(conn)
    row = conn.execute(
        "SELECT group_key, fitted_at, n_pairs, curve_json FROM reliability_curves "
        "WHERE group_key = ? AND is_active = 1 "
        "ORDER BY id DESC LIMIT 1",
        (group_key,),
    ).fetchone()
    if row is None:
        return None
    try:
        return ReliabilityCurve.from_json(
            row["curve_json"],
            group_key=row["group_key"],
            fitted_at=row["fitted_at"],
            n_pairs=int(row["n_pairs"]),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("load_active_curve: bad curve_json for group=%s", group_key, exc_info=True)
        return None


def _curve_age_days(fitted_at: str) -> float | None:
    """Age of a curve in days from its ``fitted_at`` stamp; None if unparseable."""
    from hightempbot.db.connection import parse_utc_timestamp
    parsed = parse_utc_timestamp(fitted_at)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0


def _curve_passes_guardrails(
    curve: ReliabilityCurve,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """True iff ``curve`` is trustworthy (enough pairs, not stale, not empty).

    Logs the rejection reason via pipeline_health (best-effort) when a curve
    is present but fails a guardrail, so live monitoring can see the fallback.
    """
    if curve.is_identity:
        return False
    reason: str | None = None
    if curve.n_pairs < MIN_PAIRS:
        reason = f"n_pairs={curve.n_pairs} < MIN_PAIRS={MIN_PAIRS}"
    else:
        age = _curve_age_days(curve.fitted_at)
        if age is None:
            reason = f"unparseable fitted_at={curve.fitted_at!r}"
        elif age > MAX_AGE_DAYS:
            reason = f"age={age:.1f}d > MAX_AGE_DAYS={MAX_AGE_DAYS}"
    if reason is None and curve.curve_type == CURVE_TYPE_BLEND:
        dreason = blend_degenerate_reason(curve.coefs)
        if dreason is not None:
            reason = f"degenerate blend ({dreason})"
    if reason is not None:
        logger.warning(
            "reliability curve %s refused (identity fallback): %s",
            curve.group_key, reason,
        )
        if conn is not None:
            try:
                from hightempbot.db.connection import log_pipeline_health
                log_pipeline_health(
                    conn, None, "reliability", "WARNING",
                    f"curve {curve.group_key} refused ({reason}); using identity",
                )
            except Exception:
                pass
        return False
    return True


# ----------------------------------------------------------------------------
# Provider — the object the live gate holds
# ----------------------------------------------------------------------------

class ReliabilityProvider:
    """Holds validated reliability curves keyed by group and applies them.

    Construct via :meth:`load` (queries the DB, validates guardrails once) or
    directly from a curve dict for tests. ``apply(group, claimed)`` returns the
    calibrated value, falling back to identity when no *valid* curve exists for
    the group.
    """

    def __init__(self, curves: dict[str, ReliabilityCurve] | None = None) -> None:
        self._curves: dict[str, ReliabilityCurve] = dict(curves or {})

    @classmethod
    def load(
        cls,
        conn: sqlite3.Connection,
        *,
        groups: tuple[str, ...] = NO_GROUPS,
    ) -> "ReliabilityProvider":
        """Load + guardrail-validate the active curve for each group."""
        valid: dict[str, ReliabilityCurve] = {}
        try:
            ensure_reliability_table(conn)
            for group in groups:
                curve = load_active_curve(conn, group)
                if curve is not None and _curve_passes_guardrails(curve, conn=conn):
                    valid[group] = curve
        except sqlite3.Error:
            logger.warning("ReliabilityProvider.load failed; using identity for all groups", exc_info=True)
        return cls(valid)

    def has_curve(self, group_key: str) -> bool:
        return group_key in self._curves

    def curve_for(self, group_key: str) -> ReliabilityCurve:
        return self._curves.get(group_key, IDENTITY_CURVE)

    def apply(
        self,
        group_key: str | None,
        claimed: float,
        no_price: float | None = None,
    ) -> float:
        """Calibrate ``claimed`` for ``group_key``; identity when no valid curve.

        ``no_price`` is the market NO price at entry; it is required by
        ``logit_blend`` curves and ignored by isotonic curves. Callers that hold
        the entry price should always pass it — an isotonic curve discards it, so
        passing it is harmless, but a blend curve falls back to identity without
        it.
        """
        if group_key is None:
            return _clamp01(float(claimed))
        curve = self._curves.get(group_key)
        if curve is None:
            return _clamp01(float(claimed))
        return curve.apply(claimed, no_price)
