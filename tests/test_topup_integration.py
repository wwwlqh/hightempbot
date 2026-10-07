"""Cross-tick top-up integration tests.

Exercises ``_evaluate_strategy`` end-to-end with synthetic ledger fixtures
simulating prior slot state. Covers origin requirements doc acceptance
examples AE1-AE3 and AE5 (sticky-anchor sequencing, edge-gate fail-then-
resume, capital recompute) plus new behaviors introduced by the top-up
work: legacy-slot lock, dust-row guard, PENDING-as-target exposure
counting, and order-time-walker anchor alignment.

The TP/SL exit side (AE4) is covered by ``test_tp_sl_monitor.py`` —
per-row TP fires correctly today on multi-row positions because each
ledger row carries its own ``fill_price`` and the monitor is row-level.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from hightempbot.execution.strategy_constants import STRATEGY_CONFIGS
from hightempbot.decision.strategies import _compute_signal_flavors
from hightempbot.persistence.ledger import poly_fee_per_share, slot_state

from tests.conftest import eval_strategy as _eval


def slot_filled_usd(conn, **kwargs):
    """Test helper: filled-USD-only wrapper over slot_state."""
    return slot_state(conn, **kwargs)[0]


def slot_entry_top_price(conn, **kwargs):
    """Test helper: anchor-only wrapper over slot_state."""
    return slot_state(conn, **kwargs)[1]


# --------------------------------------------------------------- Fixtures

def _no_market(
    *,
    best_bid: float,
    no_ask_levels: list | None = None,
    volume: float = 1000.0,
) -> dict:
    """Build market_data[bi] for a NO-side bet at the given best_bid.

    Matches the shape used by ``test_decision_strategies.py::_bracket_market``:
    NO ask levels start at the scanner-time fill price (``best_bid`` is the
    label both sides use because the YES bid and NO ask price-mirror)."""
    if no_ask_levels is None:
        # Deep cheap NO asks at the scanner-time top, plus walk-room above.
        no_ask_levels = [
            {"price": str(best_bid), "size": "100000"},
            {"price": str(round(best_bid + 0.01, 4)), "size": "100000"},
            {"price": str(round(best_bid + 0.02, 4)), "size": "100000"},
        ]
    return {
        "best_ask": round(1.0 - best_bid, 4),
        "best_bid": best_bid,
        "volume24hr": volume,
        "market_id": "0xdeadbeef",
        "token_id": "TOK_YES",
        "no_token_id": "TOK_NO",
        "bracket_low": 20.0,
        "bracket_high": 22.0,
        "bracket_label": "20-22°C",
        "_yes_book": {"asks": [{"price": "0.05", "size": "10000"}], "bids": []},
        "_no_book": {"asks": no_ask_levels, "bids": []},
    }


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bet_ts TEXT,
            station_id TEXT,
            target_date TEXT,
            threshold REAL,
            side TEXT,
            bet_size REAL,
            event_type TEXT,
            event_detail TEXT,
            outcome TEXT
        )"""
    )
    return c


def _insert_slot_row(
    conn: sqlite3.Connection,
    *,
    bet_ts: str,
    bet_size: float,
    entry_top_price: float | None,
    entry_fill_vwap: float | None = None,
    strategy: str = "NO",
    outcome: str = "PENDING",
    side: str = "NO",
    station_id: str = "KJFK",
    target_date: str = "2026-05-10",
    threshold: float = 22.0,
    bracket_low: float | None = 20.0,
) -> int:
    """Insert one ledger row representing a prior fill on the test slot."""
    detail: dict = {"strategy": strategy, "bracket_low": bracket_low}
    if entry_top_price is not None:
        detail["entry_top_price"] = entry_top_price
    if entry_fill_vwap is not None:
        detail["entry_fill_vwap"] = entry_fill_vwap
    cur = conn.execute(
        """INSERT INTO ledger (bet_ts, station_id, target_date, threshold, side,
            bet_size, event_type, event_detail, outcome) VALUES (?,?,?,?,?,?,?,?,?)""",
        (bet_ts, station_id, target_date, threshold, side, bet_size, "bet",
         json.dumps(detail), outcome),
    )
    conn.commit()
    return int(cur.lastrowid)


def _evaluate_no(
    conn: sqlite3.Connection,
    *,
    best_bid: float,
    capital: float,
    p_emos: float = 0.11,
    no_ask_levels: list | None = None,
) -> object:
    """Run ``_evaluate_strategy`` for the NO strategy on the standard slot."""
    flavors = _compute_signal_flavors(p_emos, 50, 10)
    return _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_no_market(best_bid=best_bid, no_ask_levels=no_ask_levels),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22°C", bracket_unit="F",
        p_emos=p_emos, pred_bucket=(0.10, 0.15), flavors=flavors,
        n_cum=50, capital=capital, local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )


def _evaluate_tail(
    conn: sqlite3.Connection,
    *,
    best_ask: float = 0.02,
    capital: float = 100.0,
    yes_ask_levels: list | None = None,
    tail_prob: float = 0.15,
) -> object:
    if yes_ask_levels is None:
        yes_ask_levels = [{"price": str(best_ask), "size": "100000"}]
    flavors = {
        "p_E": tail_prob,
        "p_B_50": tail_prob,
        "p_L_loose": tail_prob,
        "p_Shrink_n10": tail_prob,
        "tail_vote_avg": tail_prob,
    }
    market = {
        "best_ask": best_ask,
        "best_bid": round(1.0 - best_ask, 4),
        "volume24hr": 1000.0,
        "market_id": "0xtail",
        "token_id": "TOK_YES",
        "no_token_id": "TOK_NO",
        "bracket_low": 20.0,
        "bracket_high": 22.0,
        "bracket_label": "20-22C",
        "_yes_book": {"asks": yes_ask_levels, "bids": []},
        "_no_book": {"asks": [{"price": "0.80", "size": "10000"}], "bids": []},
    }
    return _eval(
        STRATEGY_CONFIGS["TAIL"], "TAIL",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=market,
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22C", bracket_unit="F",
        p_emos=tail_prob, pred_bucket=(0.0, 0.1), flavors=flavors,
        # TAIL entry hour moved {0} -> {1} by the L2 champion (2026-05-29).
        n_cum=50, capital=capital, local_now_hour=1,
        conn=conn, ledger_event_types=("bet",),
    )


# --------------------------------------------------------- AE1: multi-tick top-up

def test_ae1_first_fill_then_top_up_uses_sticky_anchor(conn: sqlite3.Connection) -> None:
    """AE1 leg 1+2: first-fill records the scanner-time anchor; the second
    tick on the same slot reuses it instead of re-anchoring to the new top.

    Both ticks use ``best_bid=0.75`` (so the NO edge gate stays in band on
    both); the test pins anchor stickiness specifically, not edge-tracking."""
    # Tick 1: empty slot — first fill should set entry_top_price=0.75
    # and stamp slot_filled_pre=0 on the signal.
    sig1 = _evaluate_no(conn, best_bid=0.75, capital=10000.0)
    assert sig1 is not None
    assert sig1.passed_all_gates
    assert sig1.slot_filled_pre == 0.0
    assert sig1.entry_top_price == 0.75

    # Persist that fill as a prior row (simulating record_bet having run).
    _insert_slot_row(
        conn, bet_ts="2026-05-10 10:00:00",
        bet_size=300.0, entry_top_price=0.75,
    )

    # Tick 2: same scanner-time top; top-up should carry the slot's
    # first-fill anchor (0.75) read back from event_detail, not recompute it.
    sig2 = _evaluate_no(conn, best_bid=0.75, capital=10000.0)
    assert sig2 is not None
    assert sig2.passed_all_gates
    assert sig2.slot_filled_pre == 300.0
    assert sig2.entry_top_price == 0.75


def test_top_up_anchor_stays_sticky_across_minor_drift(conn: sqlite3.Connection) -> None:
    """When the scanner-time top has drifted up but the slot has prior
    exposure, the anchor must remain the slot's original first-fill
    anchor, not the new (drifted) top."""
    # Slot has prior fill at original anchor=0.75.
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=200.0, entry_top_price=0.75)
    # Scanner-time best_bid drifted up to 0.76 — the new top differs from
    # the recorded anchor. Stickiness requires sig.entry_top_price == 0.75
    # (the slot's first-fill anchor read back from event_detail), not 0.76.
    sig = _evaluate_no(conn, best_bid=0.76, capital=10000.0)
    assert sig is not None
    assert sig.entry_top_price == 0.75  # sticky anchor preserved across drift
    assert sig.slot_filled_pre == 200.0


def test_ae1_top_up_target_subtracts_slot_filled(conn: sqlite3.Connection) -> None:
    """AE1: remaining target = capital_frac × capital − slot_filled."""
    # Prior tick filled $300 of a NO slot. At capital=10000 the full target
    # is 0.07 × 10000 = $700, so remaining = $400.
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=300.0, entry_top_price=0.75)
    sig = _evaluate_no(conn, best_bid=0.75, capital=10000.0)
    assert sig is not None
    assert sig.passed_all_gates
    assert sig.slot_filled_pre == 300.0
    # bet_size_usd is the realized walker fill — bounded by remaining ($400).
    assert sig.bet_size_usd <= 400.0 + 1e-6
    assert sig.bet_size_usd > 0


def test_no_first_fill_uses_vwap_edge_floor_not_five_cent_leash(conn: sqlite3.Connection) -> None:
    sig = _evaluate_no(
        conn,
        best_bid=0.75,
        capital=100.0,
        p_emos=0.091,
        no_ask_levels=[
            {"price": "0.75", "size": "7"},
            {"price": "0.88", "size": "1000"},
        ],
    )

    assert sig is not None
    assert sig.passed_all_gates
    assert sig.bet_size_usd == pytest.approx(7.0)
    assert sig.limit_price > 0.75
    assert sig.edge >= 0.05


def test_no_preserves_entry_edge_when_walking_to_five_pct_vwap_floor(conn: sqlite3.Connection) -> None:
    """Entry passes at 9%+; execution can now walk below the entry edge down to
    the 5% realized-VWAP floor (L2 champion: NO execution_min_edge 0.03 -> 0.05).
    The walked level (0.91) realizes ~5.3pp — above the floor — so the fill
    places; a level realizing <5pp would be left on the book.
    """
    p_emos = 0.0325505153150214
    prob_safe_floor = 1.0 - p_emos
    entry_price = 0.87
    walked_price = 0.91
    entry_edge = prob_safe_floor - entry_price - poly_fee_per_share(entry_price)
    walked_edge = prob_safe_floor - walked_price - poly_fee_per_share(walked_price)

    assert entry_edge == pytest.approx(0.0917944847)
    assert walked_edge == pytest.approx(0.0533544847)
    assert walked_edge >= 0.05  # at/above the new NO execution_min_edge floor

    sig = _evaluate_no(
        conn,
        best_bid=entry_price,
        capital=100.0,
        p_emos=p_emos,
        no_ask_levels=[{"price": str(walked_price), "size": "100000"}],
    )

    assert sig is not None
    assert sig.passed_all_gates
    assert sig.p_market == pytest.approx(entry_price)
    assert sig.fill_price == pytest.approx(entry_price)
    assert sig.edge == pytest.approx(entry_edge)
    assert sig.limit_price == pytest.approx(walked_price)
    assert sig.bet_size_usd == pytest.approx(7.0)


def test_no_vwap_edge_floor_stops_before_bad_depth(conn: sqlite3.Connection) -> None:
    sig = _evaluate_no(
        conn,
        best_bid=0.75,
        capital=100.0,
        p_emos=0.091,
        no_ask_levels=[
            {"price": "0.75", "size": "1.36"},
            {"price": "0.99", "size": "1000"},
        ],
    )

    assert sig is not None
    assert sig.passed_all_gates
    assert sig.bet_size_usd == pytest.approx(1.02)
    assert sig.limit_price == pytest.approx(0.75)
    assert sig.edge >= 0.05


def test_tail_first_fill_uses_seven_pct_vwap_edge_floor_not_five_cent_leash(conn: sqlite3.Connection) -> None:
    """TAIL fills by the realized-VWAP floor (L2 champion: 0.03 -> 0.07), NOT a
    fixed 5-cent leash. top_ask=0.02 would cap a leash at 0.07, but with a high
    enough tail probability the walker pushes PAST 0.07 to the 0.10 level while
    the realized VWAP edge stays >= 7pp.
    """
    sig = _evaluate_tail(
        conn,
        best_ask=0.02,
        capital=100.0,
        tail_prob=0.30,
        yes_ask_levels=[
            {"price": "0.02", "size": "1"},
            {"price": "0.10", "size": "1000"},
        ],
    )

    assert sig is not None
    assert sig.passed_all_gates
    assert sig.bet_size_usd == pytest.approx(5.0)
    assert sig.limit_price > 0.07  # walked past the 5-cent leash (top_ask 0.02 + 0.05)
    assert sig.edge == pytest.approx(0.30 - 0.02 - poly_fee_per_share(0.02))
    assert sig.edge >= 0.07


def test_tail_vwap_edge_floor_stops_before_bad_depth(conn: sqlite3.Connection) -> None:
    sig = _evaluate_tail(
        conn,
        best_ask=0.02,
        capital=100.0,
        tail_prob=0.095,
        yes_ask_levels=[
            {"price": "0.02", "size": "10"},
            {"price": "1.00", "size": "1000"},
        ],
    )

    assert sig is not None
    assert not sig.passed_all_gates
    assert sig.gate_results.get("insufficient_depth") is False


# ------------------------------------------------------- AE3: edge gate cycles

def test_ae3_edge_gate_fail_does_not_lock_slot(conn: sqlite3.Connection) -> None:
    """When the current tick's edge moves out of the gate band but the slot
    has prior exposure, the bet is skipped without closing the slot — a
    later tick can resume top-up when the gate is back in band."""
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=150.0, entry_top_price=0.75)
    # Edge moves out of band: p_emos=0.50 makes NO edge way too low.
    sig_fail = _evaluate_no(conn, best_bid=0.75, capital=10000.0, p_emos=0.50)
    assert sig_fail is not None
    # The edge gate fails, so passed_all_gates is False. The slot stays open.
    assert not sig_fail.passed_all_gates
    # Verify the slot wasn't marked legacy-locked or otherwise terminally blocked.
    assert sig_fail.gate_results.get("legacy_slot_locked") is not True

    # When the gate comes back into band, a top-up can fire.
    sig_resume = _evaluate_no(conn, best_bid=0.75, capital=10000.0, p_emos=0.11)
    assert sig_resume is not None
    assert sig_resume.passed_all_gates
    assert sig_resume.slot_filled_pre == 150.0


# ----------------------------------------------------- AE5: capital recompute

def test_ae5_capital_change_between_ticks_recomputes_target(conn: sqlite3.Connection) -> None:
    """If capital changes between ticks (resolution lands mid-window), the
    next top-up sizes against the new capital, not the old one."""
    # Prior fill at $560 (would have been $700 target at capital=10000).
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=560.0, entry_top_price=0.75)
    # New capital is 8000 → target_usd = 0.07 × 8000 = $560 → remaining = $0
    # → dust guard fires (remaining < max(MIN_BET_USD, 1% × $560 = $5.60) = $5.60).
    sig = _evaluate_no(conn, best_bid=0.75, capital=8000.0)
    assert sig is not None
    assert not sig.passed_all_gates
    assert sig.gate_results.get("idempotency") is False


# ------------------------------------------------- Legacy-slot lock

def test_legacy_slot_lock_skips_when_anchor_missing(conn: sqlite3.Connection) -> None:
    """A slot with prior fills written before the top-up rollout has no
    ``entry_top_price`` in event_detail. The new gate must skip it rather
    than silently re-anchor (origin Scope Boundary: forward-only)."""
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=300.0, entry_top_price=None)  # legacy row
    sig = _evaluate_no(conn, best_bid=0.75, capital=10000.0)
    assert sig is not None
    assert not sig.passed_all_gates
    assert sig.gate_results.get("legacy_slot_locked") is True
    assert sig.gate_results.get("idempotency") is False


def test_legacy_lock_does_not_fire_on_empty_slot(conn: sqlite3.Connection) -> None:
    """Cold-start with no prior rows must NOT fire legacy-slot-lock — the
    reader returns None but slot_filled is 0, so the cold-start path uses
    the scanner-time anchor."""
    sig = _evaluate_no(conn, best_bid=0.75, capital=10000.0)
    assert sig is not None
    assert sig.passed_all_gates
    assert sig.gate_results.get("legacy_slot_locked") is not True
    assert sig.entry_top_price == 0.75


# ------------------------------------------------- Dust-row guard

def test_dust_row_guard_blocks_small_top_up(conn: sqlite3.Connection) -> None:
    """At a $700 target, remaining < $7 (1% floor) should skip the top-up
    rather than fire a dust row."""
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=695.0, entry_top_price=0.75)
    # remaining = 700 - 695 = $5 < effective_min_bet = max(1.0, 7.0) = $7
    sig = _evaluate_no(conn, best_bid=0.75, capital=10000.0)
    assert sig is not None
    assert not sig.passed_all_gates
    assert sig.gate_results.get("idempotency") is False


def test_dust_floor_at_min_bet_usd_for_tiny_targets(conn: sqlite3.Connection) -> None:
    """At a $1000-capital × 0.07 = $70 target, the dust floor is
    max(MIN_BET_USD=1.0, 0.7) = $1.0. So remaining $0.30 should be skipped."""
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=69.70, entry_top_price=0.75)
    sig = _evaluate_no(conn, best_bid=0.75, capital=1000.0)
    assert sig is not None
    assert not sig.passed_all_gates
    assert sig.gate_results.get("idempotency") is False


def test_precision_grid_dust_top_up_is_skipped_before_order_submit(conn: sqlite3.Connection) -> None:
    """A top-up above $1 can still be unorderable after CLOB price/size rounding."""
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=9.8242284, entry_top_price=0.882)

    sig = _evaluate_no(
        conn,
        best_bid=0.882,
        capital=181.42284,
        p_emos=0.009779189206727046,
        no_ask_levels=[{"price": "0.898", "size": "100000"}],
    )

    assert sig is not None
    assert not sig.passed_all_gates
    assert sig.gate_results.get("idempotency") is True
    assert sig.gate_results.get("insufficient_size") is False


# -------------------------------------------- PENDING counts as exposure

def test_pending_row_counts_as_exposure(conn: sqlite3.Connection) -> None:
    """A still-PENDING first fill must count toward slot_filled_usd so
    the next tick doesn't race into additive over-stake before the
    reconciler corrects bet_size from target to realized."""
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=300.0, entry_top_price=0.75,
                     outcome="PENDING")
    filled = slot_filled_usd(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="NO",
        bracket_low_val=20.0, strategy="NO",
        ledger_event_types=("bet",),
    )
    assert filled == 300.0


def test_cancelled_row_does_not_count(conn: sqlite3.Connection) -> None:
    """A CANCELLED row (failed order, etc.) must not contribute to exposure."""
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=300.0, entry_top_price=0.75,
                     outcome="CANCELLED")
    filled = slot_filled_usd(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="NO",
        bracket_low_val=20.0, strategy="NO",
        ledger_event_types=("bet",),
    )
    assert filled == 0.0


# -------------------------------------------- slot_entry_top_price reader

def test_slot_anchor_returns_earliest_row_value(conn: sqlite3.Connection) -> None:
    """Multiple non-cancelled rows on the slot — anchor reader returns
    the EARLIEST row's value, regardless of how many top-ups followed."""
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=150.0, entry_top_price=0.75)
    _insert_slot_row(conn, bet_ts="2026-05-10 11:00:00",
                     bet_size=100.0, entry_top_price=0.72)  # later top-up
    _insert_slot_row(conn, bet_ts="2026-05-10 12:00:00",
                     bet_size=80.0, entry_top_price=0.73)   # later top-up
    anchor = slot_entry_top_price(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="NO",
        bracket_low_val=20.0, strategy="NO",
        ledger_event_types=("bet",),
    )
    assert anchor == 0.75  # earliest row wins


def test_slot_anchor_returns_none_when_no_rows(conn: sqlite3.Connection) -> None:
    anchor = slot_entry_top_price(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="NO",
        bracket_low_val=20.0, strategy="NO",
        ledger_event_types=("bet",),
    )
    assert anchor is None


def test_slot_anchor_returns_none_when_legacy_row_lacks_key(conn: sqlite3.Connection) -> None:
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=300.0, entry_top_price=None)
    anchor = slot_entry_top_price(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="NO",
        bracket_low_val=20.0, strategy="NO",
        ledger_event_types=("bet",),
    )
    assert anchor is None


# ----------------------------------------- AE2: window-close blocks top-up

def test_ae2_window_close_blocks_top_up(conn: sqlite3.Connection) -> None:
    """When the local hour falls outside NO's entry_hour_set, the hour gate
    fires (returns None) before the slot query runs — no top-up regardless
    of prior exposure or anchor."""
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=300.0, entry_top_price=0.65)
    flavors = _compute_signal_flavors(0.19, 50, 10)
    sig = _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_no_market(best_bid=0.75),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22°C", bracket_unit="F",
        p_emos=0.11, pred_bucket=(0.10, 0.15), flavors=flavors,
        n_cum=50, capital=10000.0,
        local_now_hour=7,  # outside frozenset(range(7)) = {0..6}
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is None


# ------------------------------- AE2: VWAP floor replaces sticky price cap

def test_ae2_no_top_up_can_ignore_sticky_price_cap_when_vwap_edge_holds(conn: sqlite3.Connection) -> None:
    """NO keeps the sticky anchor for audit, but the fill boundary is now
    realized VWAP edge >= 5%, not entry_top_price + 0.05."""
    _insert_slot_row(conn, bet_ts="2026-05-10 10:00:00",
                     bet_size=5800.0, entry_top_price=0.70)
    # Old sticky cap would be 0.70 + 0.05 = 0.75. best_bid=0.76 is above that
    # old cap, but the cumulative VWAP still has more than 5pp edge.
    # p_emos=0.10 keeps the NO edge gate in band at fp=0.71:
    # edge = (1-0.10) - 0.71 - fee ~= 0.180 ∈ [0.09, 0.25].
    mkt = _no_market(
        best_bid=0.76,
        no_ask_levels=[
            {"price": "0.76", "size": "100000"},
            {"price": "0.77", "size": "100000"},
        ],
    )
    p_emos = 0.085
    flavors = _compute_signal_flavors(p_emos, 50, 10)
    sig = _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=mkt,
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22°C", bracket_unit="F",
        p_emos=p_emos, pred_bucket=(0.05, 0.15), flavors=flavors,
        n_cum=50, capital=100000.0, local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    # remaining = 0.07 × 100000 − 5800 = $1200, well above the $70 dust floor.
    assert sig.gate_results.get("idempotency") is True
    assert sig.passed_all_gates
    assert sig.bet_size_usd == pytest.approx(1200.0)
    assert sig.limit_price == pytest.approx(0.76)
    assert sig.edge >= 0.05


# ------------------------ slot isolation across different brackets

def test_slot_predicate_isolates_different_brackets(conn: sqlite3.Connection) -> None:
    """slot_filled_usd must scope by bracket_low. Two slots sharing
    (station, date, threshold, side, strategy) but with different
    bracket_low values must be counted independently. A bracket_low_val=None
    query targets the floor-bracket slot (event_detail.bracket_low IS NULL)
    and must NOT pick up rows with explicit bracket_low values."""
    _insert_slot_row(
        conn, bet_ts="2026-05-10 10:00:00",
        bet_size=400.0, entry_top_price=0.75, bracket_low=20.0,
    )
    _insert_slot_row(
        conn, bet_ts="2026-05-10 10:05:00",
        bet_size=100.0, entry_top_price=0.75, bracket_low=22.0,
    )
    assert slot_filled_usd(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="NO",
        bracket_low_val=20.0, strategy="NO",
        ledger_event_types=("bet",),
    ) == 400.0
    assert slot_filled_usd(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="NO",
        bracket_low_val=22.0, strategy="NO",
        ledger_event_types=("bet",),
    ) == 100.0
    assert slot_filled_usd(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="NO",
        bracket_low_val=None, strategy="NO",
        ledger_event_types=("bet",),
    ) == 0.0


# ----------------------- NO top-up edge-floor behavior (slip leash removed 2026-05-31)

def test_no_topup_walks_to_edge_floor_without_slip_leash(conn: sqlite3.Connection) -> None:
    """A NO top-up no longer stops at first_fill_vwap + 0.04.

    The prior first-fill VWAP is still available for audit, but fill depth is
    now bounded by the current entry gates plus the 5pp realized VWAP edge floor.
    The 0.82 level clears that floor, so the top-up can consume it.
    """
    _insert_slot_row(
        conn, bet_ts="2026-05-10 10:00:00", bet_size=1.0,
        entry_top_price=0.75, entry_fill_vwap=0.75,
    )
    sig = _evaluate_no(
        conn, best_bid=0.75, capital=100.0,
        no_ask_levels=[
            {"price": "0.76", "size": "3"},
            {"price": "0.82", "size": "100000"},
        ],
    )
    assert sig is not None
    assert sig.passed_all_gates
    assert sig.slot_filled_pre == pytest.approx(1.0)        # this is a top-up
    assert sig.limit_price == pytest.approx(0.82)           # no slip leash
    assert sig.bet_size_usd == pytest.approx(6.0)           # remaining target


def test_no_topup_book_above_old_slip_bound_still_fills_if_edge_allows(
    conn: sqlite3.Connection,
) -> None:
    """The removed 0.04 slip leash must not reject a valid edge-floor fill."""
    _insert_slot_row(
        conn, bet_ts="2026-05-10 10:00:00", bet_size=1.0,
        entry_top_price=0.75, entry_fill_vwap=0.75,
    )
    sig = _evaluate_no(
        conn, best_bid=0.75, capital=100.0,
        no_ask_levels=[{"price": "0.82", "size": "100000"}],
    )
    assert sig is not None
    assert sig.passed_all_gates
    assert sig.slot_filled_pre == pytest.approx(1.0)
    assert sig.limit_price == pytest.approx(0.82)
    assert sig.bet_size_usd == pytest.approx(6.0)


def test_no_topup_null_fill_vwap_anchor_falls_back_without_reanchor(
    conn: sqlite3.Connection,
) -> None:
    """NULL-anchor guard: a transition slot (entry_top_price present,
    entry_fill_vwap absent) falls back to normal NO behavior: current gates
    plus the 5pp realized VWAP edge floor. The 0.82 level is consumed.
    """
    _insert_slot_row(
        conn, bet_ts="2026-05-10 10:00:00", bet_size=1.0,
        entry_top_price=0.75, entry_fill_vwap=None,   # transition slot
    )
    sig = _evaluate_no(
        conn, best_bid=0.75, capital=100.0,
        no_ask_levels=[
            {"price": "0.76", "size": "3"},
            {"price": "0.82", "size": "100000"},
        ],
    )
    assert sig is not None
    assert sig.passed_all_gates
    assert sig.slot_filled_pre == pytest.approx(1.0)
    assert sig.limit_price == pytest.approx(0.82)


def test_no_first_fill_walks_to_edge_floor_and_records_fill_vwap(conn: sqlite3.Connection) -> None:
    """First fill walks to the edge floor and records the walked VWAP.

    The recorded value remains useful for audit even though NO top-ups no
    longer use it as a slip cap.
    """
    sig = _evaluate_no(
        conn, best_bid=0.75, capital=100.0,
        no_ask_levels=[{"price": "0.82", "size": "100000"}],
    )
    assert sig is not None
    assert sig.passed_all_gates
    assert sig.slot_filled_pre == 0.0                       # first fill
    assert sig.limit_price == pytest.approx(0.82)
    assert sig.entry_fill_vwap == pytest.approx(0.82)
