"""Tests for the reliability-calibration layer + °C quarantine gate."""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hightempbot.calibration.reliability import (
    BLEND_MAX_PRICE_WEIGHT,
    CURVE_TYPE_BLEND,
    CURVE_TYPE_ISOTONIC,
    MAX_AGE_DAYS,
    MIN_PAIRS,
    ReliabilityCurve,
    ReliabilityProvider,
    _logit,
    _sigmoid,
    blend_degenerate_reason,
    fit_isotonic,
    fit_logit_blend,
    group_for_unit,
    load_active_curve,
    save_curve,
)
from hightempbot.db.connection import get_connection, init_db
from hightempbot.decision import strategies as strat
from hightempbot.execution.strategy_constants import STRATEGY_CONFIGS
from hightempbot.decision.strategies import _compute_signal_flavors

from tests.conftest import eval_strategy as _eval


# ---------------------------------------------------------------- PAV / fit

def _is_monotone(vals: list[float]) -> bool:
    return all(vals[i] <= vals[i + 1] + 1e-12 for i in range(len(vals) - 1))


def test_fit_isotonic_empty() -> None:
    assert fit_isotonic([]) == ([], [])


def test_fit_isotonic_already_monotone() -> None:
    bps, vals = fit_isotonic([(0.1, 0), (0.2, 0), (0.3, 1), (0.4, 1)])
    assert bps == [0.1, 0.2, 0.3, 0.4]
    assert vals == [0.0, 0.0, 1.0, 1.0]
    assert _is_monotone(vals)


def test_fit_isotonic_merges_violators() -> None:
    # 0.1->1 then 0.2->0 violates monotonicity; PAV pools to 0.5 each.
    bps, vals = fit_isotonic([(0.1, 1), (0.2, 0)])
    assert bps == [0.1, 0.2]
    assert vals == [0.5, 0.5]


def test_fit_isotonic_pools_ties() -> None:
    # Three points at the same x must pool to a single block (single-valued).
    bps, vals = fit_isotonic([(0.5, 1), (0.5, 0), (0.5, 1)])
    assert bps == [0.5]
    assert vals == pytest.approx([2.0 / 3.0])


def test_fit_isotonic_random_always_monotone() -> None:
    rng = random.Random(42)
    for _ in range(50):
        pairs = [(rng.random(), rng.randint(0, 1)) for _ in range(rng.randint(1, 40))]
        bps, vals = fit_isotonic(pairs)
        assert _is_monotone(vals)
        assert bps == sorted(bps)  # strictly increasing breakpoints
        assert len(set(bps)) == len(bps)


def test_curve_apply_interpolates_and_clamps() -> None:
    curve = ReliabilityCurve.fit(
        [(0.7, 0), (0.8, 0), (0.9, 1), (0.95, 1), (0.99, 1)], group_key="NO_F"
    )
    # Between 0.8(->0) and 0.9(->1): midpoint 0.85 -> 0.5.
    assert curve.apply(0.85) == pytest.approx(0.5)
    # Below/above range -> flat extrapolation, clamped to [0,1].
    assert curve.apply(0.0) == pytest.approx(0.0)
    assert curve.apply(1.0) == pytest.approx(1.0)
    assert 0.0 <= curve.apply(-5.0) <= 1.0
    assert 0.0 <= curve.apply(5.0) <= 1.0


def test_identity_curve_apply() -> None:
    curve = ReliabilityCurve()
    assert curve.is_identity
    assert curve.apply(0.83) == pytest.approx(0.83)
    # NaN input coerces safely to 0.0 rather than propagating.
    assert curve.apply(float("nan")) == 0.0


def test_group_for_unit() -> None:
    assert group_for_unit("F") == "NO_F"
    assert group_for_unit("C") == "NO_C"
    assert group_for_unit("") is None
    assert group_for_unit("K") is None
    assert group_for_unit("F", side="YES") is None


# ------------------------------------------------ market-aware logit blend

def _synth_blend_triples(
    n: int, *, b0: float, b1: float, b2: float, seed: int = 7
) -> list[tuple[float, float, float]]:
    """Generate (claimed, no_price, won) where won ~ Bernoulli(sigmoid(b0 + b1·
    logit(claimed) + b2·logit(price)))."""
    rng = random.Random(seed)
    out: list[tuple[float, float, float]] = []
    for _ in range(n):
        claimed = rng.uniform(0.70, 0.99)
        price = rng.uniform(0.55, 0.95)
        prob = _sigmoid(b0 + b1 * _logit(claimed) + b2 * _logit(price))
        won = 1.0 if rng.random() < prob else 0.0
        out.append((claimed, price, won))
    return out


def test_fit_logit_blend_recovers_direction() -> None:
    # True model leans on the market price (b2 > b1 > 0).
    triples = _synth_blend_triples(6000, b0=-1.0, b1=1.0, b2=2.0)
    coefs = fit_logit_blend(triples)
    assert len(coefs) == 3
    b0, b1, b2 = coefs
    # Directional recovery: both slopes positive, price slope the larger one,
    # intercept negative — matching the generative process.
    assert b1 > 0.0
    assert b2 > 0.0
    assert b2 > b1
    assert b0 < 0.0
    # Coarse magnitude sanity (not exact — finite sample + ridge).
    assert 0.5 < b1 < 1.6
    assert 1.4 < b2 < 2.7


def test_fit_logit_blend_degenerate_returns_empty() -> None:
    # Too few triples -> no fit.
    assert fit_logit_blend([(0.9, 0.8, 1.0), (0.8, 0.7, 0.0)]) == []
    # All-won labels -> no identifiable slope -> empty.
    allwon = [(0.9, 0.8, 1.0)] * 50
    assert fit_logit_blend(allwon) == []


def test_blend_curve_apply_uses_price() -> None:
    curve = ReliabilityCurve(
        curve_type=CURVE_TYPE_BLEND, coefs=[0.0, 0.5, 1.5], group_key="NO_F"
    )
    assert not curve.is_identity
    # b2 > 0: a higher market NO price yields a higher calibrated probability.
    lo = curve.apply(0.90, 0.70)
    hi = curve.apply(0.90, 0.90)
    assert hi > lo
    # Matches the closed-form blend.
    assert curve.apply(0.90, 0.80) == pytest.approx(
        _sigmoid(0.0 + 0.5 * _logit(0.90) + 1.5 * _logit(0.80))
    )
    # No price supplied -> identity fallback (can't evaluate the blend).
    assert curve.apply(0.90) == pytest.approx(0.90)
    assert curve.apply(0.90, float("nan")) == pytest.approx(0.90)
    # Empty coefs -> identity.
    empty = ReliabilityCurve(curve_type=CURVE_TYPE_BLEND, coefs=[])
    assert empty.is_identity
    assert empty.apply(0.83, 0.80) == pytest.approx(0.83)


def test_blend_curve_json_round_trip() -> None:
    curve = ReliabilityCurve.fit_blend(
        _synth_blend_triples(500, b0=-0.5, b1=1.0, b2=1.5),
        group_key="NO_F",
    )
    raw = curve.to_json()
    assert '"logit_blend"' in raw
    loaded = ReliabilityCurve.from_json(raw)
    assert loaded.curve_type == CURVE_TYPE_BLEND
    assert loaded.coefs == pytest.approx(curve.coefs)
    assert loaded.group_key == "NO_F"
    # Same output at the same (claimed, price).
    assert loaded.apply(0.92, 0.80) == pytest.approx(curve.apply(0.92, 0.80))


def test_from_json_backward_compat_isotonic() -> None:
    # A legacy payload with NO "type" key must load as an isotonic curve and
    # apply exactly as before (price ignored).
    legacy = (
        '{"breakpoints": [0.7, 0.8, 0.9], "values": [0.0, 0.0, 1.0], '
        '"n_pairs": 300, "fitted_at": "2026-07-16 00:00:00", "group_key": "NO_F"}'
    )
    curve = ReliabilityCurve.from_json(legacy)
    assert curve.curve_type == CURVE_TYPE_ISOTONIC
    assert curve.breakpoints == [0.7, 0.8, 0.9]
    # Between 0.8(->0) and 0.9(->1): midpoint 0.85 -> 0.5; passing a price is a
    # no-op for isotonic.
    assert curve.apply(0.85) == pytest.approx(0.5)
    assert curve.apply(0.85, 0.80) == pytest.approx(0.5)


def test_blend_degenerate_reason() -> None:
    # Healthy: positive slopes, claim keeps meaningful weight.
    assert blend_degenerate_reason([0.0, 1.0, 1.0]) is None
    # Wrong sign on the model claim.
    assert "b1" in blend_degenerate_reason([0.0, -0.1, 1.0])
    # Wrong sign on the market price.
    assert "b2" in blend_degenerate_reason([0.0, 1.0, -0.1])
    # Price-dominant (edge collapses to ~0). price_weight = 0.99/(0.01+0.99).
    reason = blend_degenerate_reason([0.0, 0.01, 0.99])
    assert reason is not None and "price_weight" in reason
    # Boundary: exactly at the threshold is allowed (strict >).
    pw = BLEND_MAX_PRICE_WEIGHT
    b2 = pw
    b1 = 1.0 - pw
    assert abs(b2) / (abs(b1) + abs(b2)) == pytest.approx(pw)
    assert blend_degenerate_reason([0.0, b1, b2]) is None
    # Empty coefs.
    assert blend_degenerate_reason([]) is not None


# ---------------------------------------------------------- persistence

@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "reliability.db"
    init_db(str(db_path))
    conn = get_connection(str(db_path))
    yield conn
    conn.close()


def test_curve_persistence_round_trip(db: sqlite3.Connection) -> None:
    curve = ReliabilityCurve.fit(
        [(0.7, 0), (0.8, 1), (0.9, 1)], group_key="NO_F"
    )
    save_curve(db, curve)
    loaded = load_active_curve(db, "NO_F")
    assert loaded is not None
    assert loaded.breakpoints == curve.breakpoints
    assert loaded.values == curve.values
    assert loaded.n_pairs == curve.n_pairs
    assert loaded.group_key == "NO_F"


def test_save_curve_deactivates_prior(db: sqlite3.Connection) -> None:
    first = ReliabilityCurve.fit([(0.7, 0), (0.9, 1)], group_key="NO_F")
    second = ReliabilityCurve.fit([(0.8, 1), (0.95, 1)], group_key="NO_F")
    save_curve(db, first)
    save_curve(db, second)
    # Exactly one active row, and it is the second curve.
    active = db.execute(
        "SELECT COUNT(*) AS n FROM reliability_curves WHERE group_key='NO_F' AND is_active=1"
    ).fetchone()["n"]
    assert active == 1
    loaded = load_active_curve(db, "NO_F")
    assert loaded.breakpoints == second.breakpoints


def test_load_active_curve_missing(db: sqlite3.Connection) -> None:
    assert load_active_curve(db, "NO_C") is None


# ---------------------------------------------------------- guardrails

def _recent_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _old_ts(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def test_provider_accepts_valid_curve(db: sqlite3.Connection) -> None:
    pairs = [(0.7 + 0.0001 * i, i % 2) for i in range(MIN_PAIRS + 10)]
    curve = ReliabilityCurve.fit(pairs, group_key="NO_F", fitted_at=_recent_ts())
    save_curve(db, curve)
    provider = ReliabilityProvider.load(db)
    assert provider.has_curve("NO_F")


def test_provider_rejects_thin_curve(db: sqlite3.Connection) -> None:
    curve = ReliabilityCurve.fit(
        [(0.7, 0), (0.9, 1)], group_key="NO_F", fitted_at=_recent_ts()
    )
    assert curve.n_pairs < MIN_PAIRS
    save_curve(db, curve)
    provider = ReliabilityProvider.load(db)
    assert not provider.has_curve("NO_F")
    # Falls back to identity: apply returns the input unchanged.
    assert provider.apply("NO_F", 0.83) == pytest.approx(0.83)
    # And a health row records the fallback.
    n = db.execute(
        "SELECT COUNT(*) AS n FROM pipeline_health WHERE stage='reliability'"
    ).fetchone()["n"]
    assert n >= 1


def test_provider_rejects_stale_curve(db: sqlite3.Connection) -> None:
    pairs = [(0.7 + 0.0001 * i, i % 2) for i in range(MIN_PAIRS + 10)]
    curve = ReliabilityCurve.fit(
        pairs, group_key="NO_F", fitted_at=_old_ts(MAX_AGE_DAYS + 5)
    )
    save_curve(db, curve)
    provider = ReliabilityProvider.load(db)
    assert not provider.has_curve("NO_F")


def test_provider_identity_when_no_curve(db: sqlite3.Connection) -> None:
    provider = ReliabilityProvider.load(db)
    assert provider.apply("NO_F", 0.9) == pytest.approx(0.9)
    assert provider.apply(None, 0.9) == pytest.approx(0.9)
    # Passing a price to an identity provider is a harmless no-op.
    assert provider.apply("NO_F", 0.9, 0.80) == pytest.approx(0.9)


def test_provider_passes_price_through_to_blend() -> None:
    curve = ReliabilityCurve(
        curve_type=CURVE_TYPE_BLEND, coefs=[0.0, 0.5, 1.5],
        n_pairs=MIN_PAIRS + 1, fitted_at=_recent_ts(), group_key="NO_F",
    )
    provider = ReliabilityProvider({"NO_F": curve})
    # The provider forwards no_price to the blend; a different price -> different
    # calibrated value.
    assert provider.apply("NO_F", 0.90, 0.90) > provider.apply("NO_F", 0.90, 0.70)
    assert provider.apply("NO_F", 0.90, 0.80) == pytest.approx(curve.apply(0.90, 0.80))
    # No price -> blend identity fallback.
    assert provider.apply("NO_F", 0.90) == pytest.approx(0.90)


def test_provider_rejects_degenerate_blend(db: sqlite3.Connection) -> None:
    # Price-dominant blend (edge would collapse to ~0) must be refused at load,
    # falling back to identity rather than silently halting NO trading.
    curve = ReliabilityCurve(
        curve_type=CURVE_TYPE_BLEND, coefs=[0.0, 0.02, 1.0],
        n_pairs=MIN_PAIRS + 1, fitted_at=_recent_ts(), group_key="NO_F",
    )
    save_curve(db, curve)
    provider = ReliabilityProvider.load(db)
    assert not provider.has_curve("NO_F")
    # Identity fallback: claimed passes through unchanged even with a price.
    assert provider.apply("NO_F", 0.90, 0.80) == pytest.approx(0.90)
    # And the refusal is recorded.
    n = db.execute(
        "SELECT COUNT(*) AS n FROM pipeline_health WHERE stage='reliability'"
    ).fetchone()["n"]
    assert n >= 1


def test_provider_accepts_healthy_blend(db: sqlite3.Connection) -> None:
    curve = ReliabilityCurve(
        curve_type=CURVE_TYPE_BLEND, coefs=[0.0, 1.0, 1.0],
        n_pairs=MIN_PAIRS + 1, fitted_at=_recent_ts(), group_key="NO_F",
    )
    save_curve(db, curve)
    provider = ReliabilityProvider.load(db)
    assert provider.has_curve("NO_F")
    assert provider.curve_for("NO_F").curve_type == CURVE_TYPE_BLEND


# ---------------------------------------------------------- NO gate wiring

def _bracket_market(*, best_bid: float, volume: float = 1000.0) -> dict:
    return {
        "best_ask": 0.20,
        "best_bid": best_bid,
        "volume24hr": volume,
        "market_id": "0xdeadbeef",
        "token_id": "TOK_YES",
        "no_token_id": "TOK_NO",
        "bracket_low": 70.0,
        "bracket_high": 72.0,
        "bracket_label": "70-72°F",
        "_yes_book": {"asks": [{"price": "0.20", "size": "10000"}], "bids": []},
        "_no_book": {"asks": [{"price": str(best_bid), "size": "10000"}], "bids": []},
    }


@pytest.fixture
def ledger_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bet_ts TEXT, station_id TEXT, target_date TEXT, threshold REAL,
            side TEXT, bet_size REAL, event_type TEXT, event_detail TEXT, outcome TEXT
        )"""
    )
    return c


def _downward_curve() -> ReliabilityCurve:
    """A hand-built monotone curve mapping claimed 0.89 -> 0.80."""
    return ReliabilityCurve(
        breakpoints=[0.50, 0.89, 1.00],
        values=[0.50, 0.80, 0.90],
        n_pairs=MIN_PAIRS + 1,
        fitted_at=_recent_ts(),
        group_key="NO_F",
    )


def _no_signal(ledger_conn, *, reliability, p_emos=0.11, best_bid=0.75):
    flavors = _compute_signal_flavors(p_emos, 50, 10)
    return _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="KJFK", target_date="2026-07-10", horizon=1, bi=0,
        mkt=_bracket_market(best_bid=best_bid),
        threshold_val=72.0, bracket_low=70.0, bracket_high=72.0,
        bracket_kind="interior", bracket_label_str="70-72°F", bracket_unit="F",
        p_emos=p_emos, pred_bucket=(0.10, 0.15), flavors=flavors,
        n_cum=50, capital=10000.0, local_now_hour=0,
        conn=ledger_conn, ledger_event_types=("bet",),
        reliability=reliability,
    )


def test_no_gate_identity_provider_matches_raw(ledger_conn: sqlite3.Connection) -> None:
    # p_E=0.11 -> claimed 0.89, no_price 0.75 -> edge ~0.1306 in [0.09,0.15].
    sig = _no_signal(ledger_conn, reliability=ReliabilityProvider())
    assert sig is not None and sig.passed_all_gates
    detail_raw = sig.gate_results["claimed_raw"]
    detail_cal = sig.gate_results["claimed_calibrated"]
    assert detail_raw == pytest.approx(0.89)
    # Identity provider -> calibrated == raw.
    assert detail_cal == pytest.approx(0.89)
    assert sig.prob_safe_floor == pytest.approx(0.89)


def test_no_gate_calibration_lowers_claimed_and_blocks(ledger_conn: sqlite3.Connection) -> None:
    # Steeper curve than _downward_curve: 0.89 -> 0.78 so the calibrated edge
    # (0.78 - 0.75 - fee ~ 0.0206) falls below the 2026-07-16 min_edge of 0.04.
    curve = ReliabilityCurve(
        breakpoints=[0.50, 0.89, 1.00],
        values=[0.50, 0.78, 0.88],
        n_pairs=MIN_PAIRS + 1,
        fitted_at=_recent_ts(),
        group_key="NO_F",
    )
    provider = ReliabilityProvider({"NO_F": curve})
    sig = _no_signal(ledger_conn, reliability=provider)
    assert sig is not None
    assert sig.gate_results["claimed_raw"] == pytest.approx(0.89)
    assert sig.gate_results["claimed_calibrated"] == pytest.approx(0.78)
    assert sig.prob_safe_floor == pytest.approx(0.78)
    assert not sig.passed_all_gates
    assert sig.gate_results.get("edge_gate") is False


def test_no_gate_threads_price_to_blend(ledger_conn: sqlite3.Connection) -> None:
    # A blend curve calibrates using the market NO price (fill_price=best_bid).
    # Prove fill_price is threaded through: the recorded calibrated value must
    # equal the blend evaluated at (claimed=0.89, no_price=best_bid).
    coefs = [0.0, 0.5, 1.5]
    curve = ReliabilityCurve(
        curve_type=CURVE_TYPE_BLEND, coefs=coefs,
        n_pairs=MIN_PAIRS + 1, fitted_at=_recent_ts(), group_key="NO_F",
    )
    provider = ReliabilityProvider({"NO_F": curve})
    best_bid = 0.80  # this becomes the NO fill_price
    sig = _no_signal(ledger_conn, reliability=provider, best_bid=best_bid)
    assert sig is not None
    expected = _sigmoid(coefs[0] + coefs[1] * _logit(0.89) + coefs[2] * _logit(best_bid))
    assert sig.gate_results["claimed_raw"] == pytest.approx(0.89)
    assert sig.gate_results["claimed_calibrated"] == pytest.approx(expected)
    # The blended value depends on the price — it is NOT the raw claim nor the
    # price-free blend.
    assert sig.gate_results["claimed_calibrated"] != pytest.approx(0.89)
    assert sig.gate_results["claimed_calibrated"] != pytest.approx(curve.apply(0.89, 0.70))
    assert sig.prob_safe_floor == pytest.approx(expected)


def test_no_gate_calibration_disabled_uses_raw(
    ledger_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the global switch off, even a downward curve is ignored -> raw claimed.
    monkeypatch.setattr(strat, "RELIABILITY_CALIBRATION_ENABLED", False)
    provider = ReliabilityProvider({"NO_F": _downward_curve()})
    sig = _no_signal(ledger_conn, reliability=provider)
    assert sig is not None and sig.passed_all_gates
    assert sig.gate_results["claimed_calibrated"] == pytest.approx(0.89)
    assert sig.prob_safe_floor == pytest.approx(0.89)


# ---------------------------------------------------------- quarantine gate

def test_no_gate_quarantines_celsius(
    ledger_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tests the quarantine MECHANISM with °C disallowed, independent of the
    # live policy value (°C was re-enabled 2026-07-16 behind the blend curve).
    monkeypatch.setattr(strat, "ALLOWED_BRACKET_UNITS", frozenset({"F"}))
    flavors = _compute_signal_flavors(0.11, 50, 10)
    sig = _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="EFHK", target_date="2026-07-10", horizon=1, bi=0,
        mkt=_bracket_market(best_bid=0.75),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior", bracket_label_str="20-22°C", bracket_unit="C",
        p_emos=0.11, pred_bucket=(0.10, 0.15), flavors=flavors,
        n_cum=50, capital=10000.0, local_now_hour=0,
        conn=ledger_conn, ledger_event_types=("bet",),
        reliability=ReliabilityProvider(),
    )
    assert sig is not None
    assert sig.gate_results["unit_allowed"] is False
    assert not sig.passed_all_gates


def test_no_gate_allows_fahrenheit(ledger_conn: sqlite3.Connection) -> None:
    sig = _no_signal(ledger_conn, reliability=ReliabilityProvider())
    assert sig is not None
    assert sig.gate_results["unit_allowed"] is True
    assert sig.passed_all_gates


def test_tail_gate_quarantines_celsius(
    ledger_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TAIL conditions that would otherwise fire, on a °C bracket -> quarantined
    # when the policy disallows °C (mechanism test; live policy re-enabled °C
    # 2026-07-16 behind the blend curve).
    monkeypatch.setattr(strat, "ALLOWED_BRACKET_UNITS", frozenset({"F"}))
    p_emos = 0.45
    flavors = _compute_signal_flavors(p_emos, 50, 10)
    mkt = {
        "best_ask": 0.02, "best_bid": 0.94, "volume24hr": 1000.0,
        "market_id": "0xdeadbeef", "token_id": "TOK_YES", "no_token_id": "TOK_NO",
        "bracket_low": 20.0, "bracket_high": 22.0, "bracket_label": "20-22°C",
        "_yes_book": {"asks": [{"price": "0.02", "size": "10000"}], "bids": []},
        "_no_book": {"asks": [{"price": "0.94", "size": "10000"}], "bids": []},
    }
    sig = _eval(
        STRATEGY_CONFIGS["TAIL"], "TAIL",
        station_id="EFHK", target_date="2026-07-10", horizon=1, bi=0,
        mkt=mkt, threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior", bracket_label_str="20-22°C", bracket_unit="C",
        p_emos=p_emos, pred_bucket=(0.40, 0.60), flavors=flavors,
        n_cum=50, capital=10000.0, local_now_hour=1,
        conn=ledger_conn, ledger_event_types=("bet",),
        reliability=ReliabilityProvider(),
    )
    if sig is not None:
        assert sig.gate_results.get("unit_allowed") is False
        assert not sig.passed_all_gates
