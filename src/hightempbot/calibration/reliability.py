"""Reliability calibration for the NO gate's claimed probability.

Live, the NO claim ``1 - p_E`` was over-confident (claimed 0.945 vs realized
0.881; worst on °C). A curve per unit group (``NO_C``/``NO_F``) maps the claim
to a calibrated probability before the edge is computed. Curve types:
isotonic (PAV) or a market-aware logit blend. Curves live in
``reliability_curves``; one that is thin, stale or degenerate is ignored
(identity).
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

# Guardrails: ignore curves fit on fewer pairs or older than this.
MIN_PAIRS: int = 200
MAX_AGE_DAYS: int = 45

GROUP_NO_C = "NO_C"
GROUP_NO_F = "NO_F"
NO_GROUPS = (GROUP_NO_C, GROUP_NO_F)

# Curve types; a payload without ``type`` is isotonic.
CURVE_TYPE_ISOTONIC = "isotonic"
CURVE_TYPE_BLEND = "logit_blend"

# A blend with more slope weight than this on the market price just copies the
# market (edge ≈ 0), so it is refused.
BLEND_MAX_PRICE_WEIGHT = 0.90

# Clamp so logit(0) / logit(1) stay finite.
_LOGIT_EPS = 1e-6


def group_for_unit(bracket_unit: str, *, side: str = "NO") -> str | None:
    """``"NO_F"``/``"NO_C"`` for NO bets in °F/°C; None otherwise (no calibration)."""
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
    """Log-odds, clamped to stay finite."""
    q = min(1.0 - _LOGIT_EPS, max(_LOGIT_EPS, float(p)))
    return math.log(q / (1.0 - q))


def _sigmoid(x: float) -> float:
    """Numerically-stable logistic sigmoid (no overflow for large |x|)."""
    if x >= 0.0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)



def fit_isotonic(
    pairs: list[tuple[float, float]],
) -> tuple[list[float], list[float]]:
    """Isotonic (non-decreasing) fit of ``(claimed, won)`` pairs by pool-adjacent-violators.

    Returns ``(breakpoints, values)`` for linear interpolation; each flat block
    is emitted as its two endpoints.
    """
    if not pairs:
        return [], []
    pts = sorted(((float(c), float(w)) for c, w in pairs), key=lambda t: t[0])

    # Pool equal x first. block = [x_lo, x_hi, sum_y, weight]
    blocks: list[list[float]] = []
    for x, y in pts:
        if blocks and blocks[-1][0] == x and blocks[-1][1] == x:
            blocks[-1][2] += y
            blocks[-1][3] += 1
        else:
            blocks.append([x, x, y, 1.0])

    # Merge backwards while the last block's mean is below the previous one.
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



def fit_logit_blend(
    triples: list[tuple[float, float, float]],
    *,
    l2: float = 1e-4,
    max_iter: int = 100,
    tol: float = 1e-9,
) -> list[float]:
    """Fit ``P(won) = sigmoid(b0 + b1·logit(claimed) + b2·logit(no_price))``.

    The market price matters because the gate fires exactly where model and
    market disagree. Newton–Raphson with a small ridge on the slopes. Returns
    ``[b0, b1, b2]``, or ``[]`` with fewer than 3 triples, one label class, or
    a failed solve.
    """
    import numpy as np

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
    """Why a blend is unusable (b1 ≤ 0, b2 < 0, or price weight above
    BLEND_MAX_PRICE_WEIGHT), or None if healthy."""
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



@dataclass
class ReliabilityCurve:
    """Claimed → calibrated probability curve: isotonic (``breakpoints``/``values``)
    or logit blend (``coefs``, needs the NO price). An empty curve is identity."""

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
        """Blend curve from ``(claimed, no_price, won)``; a failed fit is identity."""
        coefs = fit_logit_blend(triples, l2=l2)
        return cls(
            curve_type=CURVE_TYPE_BLEND,
            coefs=coefs,
            n_pairs=len(triples),
            fitted_at=fitted_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            group_key=group_key,
        )

    def apply(self, claimed: float, no_price: float | None = None) -> float:
        """Calibrated probability in [0, 1]. Blends need ``no_price`` (identity without it)."""
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
        """Linear interpolation with flat extrapolation; identity when empty."""
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
        """Blend value; identity if the fit is empty or no valid price is given."""
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
        # Isotonic payloads omit ``type`` to match older saved curves.
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


IDENTITY_CURVE = ReliabilityCurve()



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
    """Save ``curve`` as the only active curve for its group. Returns the row id."""
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
    """True if the curve has enough pairs, is fresh and non-empty; logs rejections."""
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



class ReliabilityProvider:
    """Validated curves by group. Build with :meth:`load` (or a dict in tests)."""

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
        Pass ``no_price`` (blend curves need it)."""
        if group_key is None:
            return _clamp01(float(claimed))
        curve = self._curves.get(group_key)
        if curve is None:
            return _clamp01(float(claimed))
        return curve.apply(claimed, no_price)
