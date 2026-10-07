"""Per-strategy router behavior: gate logic, hour gate, idempotency, sizing."""

from __future__ import annotations

import json
import sqlite3

import pytest

from hightempbot.execution.strategy_constants import (
    SCAN_INTERVAL_MINUTES,
    STRATEGY_CONFIGS,
    icao_tick_offset,
)
from hightempbot.calibration.reliability import (
    CURVE_TYPE_BLEND,
    GROUP_NO_F,
    ReliabilityCurve,
    ReliabilityProvider,
)
from hightempbot.decision.strategies import (
    _compute_signal_flavors,
    _evaluate_flip_branch,
    _evaluate_no_branch,
)
from hightempbot.persistence.ledger import slot_state

from tests.conftest import eval_strategy as _eval


def slot_filled_usd(conn, **kwargs):
    """Test helper: thin wrapper over slot_state that returns the filled USD only."""
    return slot_state(conn, **kwargs)[0]


def _bracket_market(
    *,
    best_ask: float | None = None,
    best_bid: float | None = None,
    volume: float = 1000.0,
    yes_ask_levels: list | None = None,
    no_ask_levels: list | None = None,
) -> dict:
    """Build a fake `market_data[bi]` dict that the router can consume."""
    if yes_ask_levels is None:
        # Cheap deep YES asks: lots of size at the quoted ask price.
        yes_ask_levels = [{"price": str(best_ask if best_ask is not None else 0.05), "size": "10000"}]
    if no_ask_levels is None:
        no_ask_levels = [{"price": str(best_bid if best_bid is not None else 0.80), "size": "10000"}]
    return {
        "best_ask": best_ask,
        "best_bid": best_bid,
        "volume24hr": volume,
        "market_id": "0xdeadbeef",
        "token_id": "TOK_YES",
        "no_token_id": "TOK_NO",
        "bracket_low": 20.0,
        "bracket_high": 22.0,
        "bracket_label": "20-22Â°C",
        "_yes_book": {"asks": yes_ask_levels, "bids": []},
        "_no_book": {"asks": no_ask_levels, "bids": []},
    }


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # Minimal ledger for idempotency check.
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


# ----------------------------------------------------------------- NO strategy

def test_no_strategy_fires_with_high_p_E(conn: sqlite3.Connection) -> None:
    # NO uses raw p_E. Build a bracket where (1-p_E) - no_price - fee is
    # inside the robust-NO [0.090, 0.15] band.
    # p_E=0.11; no_price=0.75; edge ~= 0.89 - 0.75 - fee = 0.1306.
    p_emos = 0.11
    flavors = _compute_signal_flavors(p_emos, 50, 10)
    cfg = STRATEGY_CONFIGS["NO"]

    sig = _eval(
        cfg, "NO",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.20, best_bid=0.75),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22Â°C", bracket_unit="F",
        p_emos=p_emos, pred_bucket=(0.10, 0.15), flavors=flavors,
        n_cum=50, capital=10000.0, local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.strategy == "NO"
    assert sig.signal_used == "p_E"
    assert sig.signal_value == p_emos
    assert sig.passed_all_gates


def test_high_pred_bucket_does_not_hard_skip_tail(conn: sqlite3.Connection) -> None:
    # The L2 champion has a station-level TAIL consensus skip, but no separate
    # live-only pred_bucket >= 0.40 hard skip. A high forecast bucket may still
    # trade when the strategy's real gates pass and station consensus is below
    # the TAIL threshold.
    p_emos = 0.45  # lands in the 0.40-0.60 bucket
    flavors = _compute_signal_flavors(p_emos, 50, 10)
    sig = _eval(
        STRATEGY_CONFIGS["TAIL"], "TAIL",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.02, best_bid=0.94, volume=1000,
                            yes_ask_levels=[{"price": "0.02", "size": "10000"}]),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22C", bracket_unit="F",
        p_emos=p_emos, pred_bucket=(0.40, 0.60), flavors=flavors,
        n_cum=50, capital=10000.0, local_now_hour=1,
        conn=conn, ledger_event_types=("bet",),
        station_max_yes_ask=0.39,
    )
    assert sig is not None
    assert sig.strategy == "TAIL"
    assert sig.passed_all_gates


def test_no_strategy_skips_below_fp_floor(conn: sqlite3.Connection) -> None:
    # NO requires no_price >= 0.75.
    flavors = _compute_signal_flavors(0.11, 50, 10)
    sig = _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.30, best_bid=0.70),  # below 0.75
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22Â°C", bracket_unit="F",
        p_emos=0.11, pred_bucket=(0.10, 0.15), flavors=flavors,
        n_cum=50, capital=10000.0, local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    # Below fp band â†’ returns None (strategy doesn't apply).
    assert sig is None


# --------------------------------------------------------------- YMID strategy

def test_ymid_hour_gate_requires_zero(conn: sqlite3.Connection) -> None:
    """YMID direct evaluation still enforces its optimized local-hour gate."""
    flavors = _compute_signal_flavors(0.40, 100, 50)
    sig = _eval(
        STRATEGY_CONFIGS["YMID"], "YMID",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.30, best_bid=0.70),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22Â°C", bracket_unit="F",
        p_emos=0.40, pred_bucket=(0.40, 0.60), flavors=flavors,
        n_cum=100, capital=10000.0,
        local_now_hour=5,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is None


def test_ymid_ratio_gate_passes(conn: sqlite3.Connection) -> None:
    # p_Shrink_n50 should be â‰¥ 1.3 * yes_price.
    # n=100, hits=50 â†’ p_L_obs = 0.50; p_Shrink_n50 = (50 + 50*0.40) / (100+50) = 70/150 â‰ˆ 0.467.
    # yes_price=0.30 â†’ 1.3 * 0.30 = 0.39 â†’ 0.467 â‰¥ 0.39 â†’ passes.
    flavors = _compute_signal_flavors(0.40, 100, 50)
    sig = _eval(
        STRATEGY_CONFIGS["YMID"], "YMID",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.30, best_bid=0.70),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22Â°C", bracket_unit="F",
        p_emos=0.40, pred_bucket=(0.40, 0.60), flavors=flavors,
        n_cum=100, capital=10000.0,
        local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.strategy == "YMID"
    assert sig.signal_used == "p_Shrink_n50"
    assert sig.passed_all_gates


def test_ymid_ratio_gate_fails(conn: sqlite3.Connection) -> None:
    # yes_price=0.40, p_Shrink_n50â‰ˆ0.467 â†’ 0.467 < 1.3 * 0.40 = 0.52 â†’ fails ratio.
    flavors = _compute_signal_flavors(0.40, 100, 50)
    sig = _eval(
        STRATEGY_CONFIGS["YMID"], "YMID",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.40, best_bid=0.60),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22Â°C", bracket_unit="F",
        p_emos=0.40, pred_bucket=(0.40, 0.60), flavors=flavors,
        n_cum=100, capital=10000.0,
        local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.strategy == "YMID"
    assert not sig.passed_all_gates
    assert sig.gate_results["ratio_gate"] is False


# --------------------------------------------------------------- TAIL strategy

def test_tail_emits_skip_signal_when_n_below_min(conn: sqlite3.Connection) -> None:
    """F-005: at small n, p_L_loose=p_E and p_B_50=p_E; vote degenerates."""
    flavors = _compute_signal_flavors(0.10, 5, 1)  # n=5 < vote_min_n=30
    sig = _eval(
        STRATEGY_CONFIGS["TAIL"], "TAIL",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.02, best_bid=0.94, volume=1000),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22Â°C", bracket_unit="F",
        p_emos=0.10, pred_bucket=(0.05, 0.10), flavors=flavors,
        n_cum=5, capital=10000.0,
        local_now_hour=1,  # TAIL entry hour {0} -> {1} (L2 champion 2026-05-29)
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.strategy == "TAIL"
    assert not sig.passed_all_gates
    assert sig.gate_results["vote_min_n"] is False
    assert sig.gate_results["edge_gate"] is False


def test_tail_4_of_4_vote_passes(conn: sqlite3.Connection) -> None:
    # All 4 voting signals must satisfy p_i >= alpha_ratio * yes_price.
    # L2 champion (2026-05-29): alpha 4.5 -> 4.0; yes_price 0.02 -> threshold 0.08.
    # With n=50 hits=13: p_E=0.25, p_B_50~=0.255, p_L_loose=0.26,
    # p_Shrink_n10~=0.258 — all comfortably above 0.08.
    flavors = _compute_signal_flavors(0.25, 50, 13)
    assert all(flavors[k] >= 0.08 for k in ("p_E", "p_B_50", "p_L_loose", "p_Shrink_n10"))

    sig = _eval(
        STRATEGY_CONFIGS["TAIL"], "TAIL",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.02, best_bid=0.94, volume=1000,
                            yes_ask_levels=[{"price": "0.02", "size": "10000"}]),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22Â°C", bracket_unit="F",
        p_emos=0.25, pred_bucket=(0.25, 0.40), flavors=flavors,
        n_cum=50, capital=10000.0,
        local_now_hour=1,  # TAIL entry hour {0} -> {1} (L2 champion 2026-05-29)
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.strategy == "TAIL"
    assert sig.signal_used == "tail_vote_avg"
    assert sig.passed_all_gates
    # F-009: tail components stashed on gate_results for ledger persistence.
    assert "_tail_votes" in sig.gate_results
    assert set(sig.gate_results["_tail_votes"].keys()) == {"p_E", "p_B_50", "p_L_loose", "p_Shrink_n10"}


def test_tail_waits_for_delayed_entry_price_after_vote_passes(conn: sqlite3.Connection) -> None:
    """TAIL can satisfy the 3c signal band but still wait for the 2c entry trigger."""
    assert STRATEGY_CONFIGS["TAIL"].fp_max == 0.03
    assert STRATEGY_CONFIGS["TAIL"].delayed_entry_fp_max == 0.02

    flavors = _compute_signal_flavors(0.25, 50, 13)
    sig = _eval(
        STRATEGY_CONFIGS["TAIL"], "TAIL",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.025, best_bid=0.94, volume=1000,
                            yes_ask_levels=[{"price": "0.025", "size": "10000"}]),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22C", bracket_unit="F",
        p_emos=0.25, pred_bucket=(0.25, 0.40), flavors=flavors,
        n_cum=50, capital=10000.0,
        local_now_hour=1,
        conn=conn, ledger_event_types=("bet",),
    )

    assert sig is not None
    assert sig.strategy == "TAIL"
    assert sig.gate_results["fp_band"] is True
    assert sig.gate_results["edge_gate"] is True
    assert sig.gate_results["volume"] is True
    assert sig.gate_results["delayed_entry"] is False
    assert sig.gate_results["idempotency"] is None
    assert not sig.passed_all_gates


# ------------------------------------------------------- consensus_skip_threshold (2026-05-17)

def test_tail_skipped_when_station_max_yes_ask_at_or_above_threshold(conn: sqlite3.Connection) -> None:
    """TAIL emits no signal when any other bracket in the station shows YES
    best_ask >= TAIL.consensus_skip_threshold (0.40, L2 champion)."""
    assert STRATEGY_CONFIGS["TAIL"].consensus_skip_threshold == 0.40
    flavors = _compute_signal_flavors(0.25, 50, 13)

    sig = _eval(
        STRATEGY_CONFIGS["TAIL"], "TAIL",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.02, best_bid=0.94, volume=1000,
                            yes_ask_levels=[{"price": "0.02", "size": "10000"}]),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22C", bracket_unit="F",
        p_emos=0.25, pred_bucket=(0.25, 0.40), flavors=flavors,
        n_cum=50, capital=10000.0,
        local_now_hour=1,  # TAIL entry hour {0} -> {1} (L2 champion 2026-05-29)
        conn=conn, ledger_event_types=("bet",),
        # Some other bracket in this station is the heavy favorite at 0.60.
        station_max_yes_ask=0.60,
    )
    assert sig is None


def test_tail_passes_when_station_max_yes_ask_below_threshold(conn: sqlite3.Connection) -> None:
    """TAIL still fires when no bracket has crossed the consensus threshold."""
    flavors = _compute_signal_flavors(0.25, 50, 13)
    sig = _eval(
        STRATEGY_CONFIGS["TAIL"], "TAIL",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.02, best_bid=0.94, volume=1000,
                            yes_ask_levels=[{"price": "0.02", "size": "10000"}]),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22C", bracket_unit="F",
        p_emos=0.25, pred_bucket=(0.25, 0.40), flavors=flavors,
        n_cum=50, capital=10000.0,
        local_now_hour=1,  # TAIL entry hour {0} -> {1} (L2 champion 2026-05-29)
        conn=conn, ledger_event_types=("bet",),
        # Top bracket at 0.39 — below the 0.40 consensus threshold (re-cut from
        # 0.49 vs the prior 0.50 threshold, for the L2 champion's 0.40).
        station_max_yes_ask=0.39,
    )
    assert sig is not None
    assert sig.passed_all_gates


def test_tail_passes_when_station_max_yes_ask_unknown(conn: sqlite3.Connection) -> None:
    """station_max_yes_ask=None (no bracket has best_ask) does not block TAIL.
    Back-compat for callers that don't supply the kwarg."""
    flavors = _compute_signal_flavors(0.25, 50, 13)
    sig = _eval(
        STRATEGY_CONFIGS["TAIL"], "TAIL",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.02, best_bid=0.94, volume=1000,
                            yes_ask_levels=[{"price": "0.02", "size": "10000"}]),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22C", bracket_unit="F",
        p_emos=0.25, pred_bucket=(0.25, 0.40), flavors=flavors,
        n_cum=50, capital=10000.0,
        local_now_hour=1,  # TAIL entry hour {0} -> {1} (L2 champion 2026-05-29)
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.passed_all_gates


def test_no_strategy_ignores_consensus_threshold(conn: sqlite3.Connection) -> None:
    """NO has consensus_skip_threshold=None, so a high station_max_yes_ask
    must NOT block a NO bet (the gate is per-strategy by design)."""
    assert STRATEGY_CONFIGS["NO"].consensus_skip_threshold is None
    # Build flavors that pass NO's strict gate (NO additive edge).
    flavors = _compute_signal_flavors(0.11, 50, 8)  # p_E=0.11 -> 1-p=0.89, np=0.75 -> edge ~0.131
    sig = _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.20, best_bid=0.75, volume=1000),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22C", bracket_unit="F",
        p_emos=0.11, pred_bucket=(0.10, 0.15), flavors=flavors,
        n_cum=50, capital=10000.0,
        local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
        # Another bracket is at consensus level — but NO's threshold is None.
        station_max_yes_ask=0.80,
    )
    # NO must produce a signal regardless of the high-consensus state.
    assert sig is not None
    assert sig.strategy == "NO"


# ------------------------------------------------------- F-003 idempotency COALESCE

def test_slot_filled_usd_legacy_no_row_with_null_strategy_counts_as_no(conn: sqlite3.Connection) -> None:
    # Legacy ledger row with no `strategy` key: must match as 'NO' via COALESCE.
    # Identical F-003 regression â€” semantics now exposed via SUM(bet_size).
    conn.execute(
        """INSERT INTO ledger (bet_ts, station_id, target_date, threshold, side,
            bet_size, event_type, event_detail, outcome) VALUES (?,?,?,?,?,?,?,?,?)""",
        ("2026-05-10 12:00:00", "KJFK", "2026-05-10", 22.0, "NO", 100.0, "bet",
         json.dumps({"bracket_low": 20.0}), "PENDING"),
    )
    conn.commit()

    filled = slot_filled_usd(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="NO",
        bracket_low_val=20.0, strategy="NO",
        ledger_event_types=("bet",),
    )
    assert filled == 100.0


def test_slot_filled_usd_legacy_null_strategy_treated_as_no(conn: sqlite3.Connection) -> None:
    # Updated semantics 2026-05-11: legacy NULL-strategy rows are treated as
    # 'NO' via COALESCE only — they no longer over-match every same-side
    # strategy key. By the time the top-up rollout ships, the deploy boundary
    # the original 2026-05-07 cross-deploy guard protected (NULL-strategy
    # rows blocking all same-side strategies) is long past — those rows
    # have all resolved out, and scoping NULL → 'NO' lets a YES strategy
    # (YMID/TAIL/YHIGH) fire fresh against the slot without colliding
    # with stale legacy NULL exposure.
    conn.execute(
        """INSERT INTO ledger (bet_ts, station_id, target_date, threshold, side,
            bet_size, event_type, event_detail, outcome) VALUES (?,?,?,?,?,?,?,?,?)""",
        ("2026-05-10 12:00:00", "KJFK", "2026-05-10", 22.0, "YES", 50.0, "bet",
         json.dumps({"bracket_low": 20.0}), "PENDING"),
    )
    conn.commit()

    # YMID/TAIL/YHIGH no longer match a NULL-strategy YES row.
    for strategy in ("YMID", "TAIL", "YHIGH"):
        assert slot_filled_usd(
            conn,
            station_id="KJFK", target_date="2026-05-10",
            threshold_val=22.0, side="YES",
            bracket_low_val=20.0, strategy=strategy,
            ledger_event_types=("bet",),
        ) == 0.0
    # A NULL-strategy YES row would only match a NO-side query (COALESCE → 'NO')
    # if it were on the NO side. Side filter still excludes cross-side matches.
    assert slot_filled_usd(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="NO",
        bracket_low_val=20.0, strategy="NO",
        ledger_event_types=("bet",),
    ) == 0.0


def test_slot_filled_usd_explicit_strategy_match(conn: sqlite3.Connection) -> None:
    # Row with explicit strategy=YMID counts for YMID, not TAIL.
    conn.execute(
        """INSERT INTO ledger (bet_ts, station_id, target_date, threshold, side,
            bet_size, event_type, event_detail, outcome) VALUES (?,?,?,?,?,?,?,?,?)""",
        ("2026-05-10 12:00:00", "KJFK", "2026-05-10", 22.0, "YES", 25.0, "bet",
         json.dumps({"bracket_low": 20.0, "strategy": "YMID"}), "PENDING"),
    )
    conn.commit()

    assert slot_filled_usd(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="YES",
        bracket_low_val=20.0, strategy="YMID",
        ledger_event_types=("bet",),
    ) == 25.0
    assert slot_filled_usd(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="YES",
        bracket_low_val=20.0, strategy="TAIL",
        ledger_event_types=("bet",),
    ) == 0.0


def test_slot_filled_usd_excludes_cancelled(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO ledger (bet_ts, station_id, target_date, threshold, side,
            bet_size, event_type, event_detail, outcome) VALUES (?,?,?,?,?,?,?,?,?)""",
        ("2026-05-10 12:00:00", "KJFK", "2026-05-10", 22.0, "NO", 100.0, "bet",
         json.dumps({"bracket_low": 20.0, "strategy": "NO"}), "CANCELLED"),
    )
    conn.commit()

    # Cancelled rows do not contribute to exposure â€” slot can fire fresh bets.
    assert slot_filled_usd(
        conn,
        station_id="KJFK", target_date="2026-05-10",
        threshold_val=22.0, side="NO",
        bracket_low_val=20.0, strategy="NO",
        ledger_event_types=("bet",),
    ) == 0.0


# ------------------------------------------------------------- YHIGH strategy

def _ceiling_market(*, best_ask: float, best_bid: float, volume: float = 1000.0) -> dict:
    """Ceiling-bracket market data ('X-or-higher' â€” bracket_high is None)."""
    mkt = _bracket_market(best_ask=best_ask, best_bid=best_bid, volume=volume)
    mkt["bracket_high"] = None  # ceiling = no upper bound
    mkt["bracket_label"] = "â‰¥30Â°C"
    return mkt


def test_yhigh_fires_on_ceiling_with_p_b_50(conn: sqlite3.Connection) -> None:
    # YHIGH gates: side=YES, ceiling-only, signal=p_B_50, fp [0.50, 1.00],
    # additive edge in [0.02, 0.30].
    # n=100, hits=70 â†’ p_L_obs=0.70, p_B_50 = 0.5*p_E + 0.5*p_L_loose.
    # With p_E=0.70 and p_L_loose=0.70 â†’ p_B_50=0.70.
    # yes_price=0.62 â†’ edge = 0.70 - 0.62 - fee(0.62) = 0.70-0.62-0.05*0.62*0.38
    # â‰ˆ 0.70 - 0.62 - 0.0118 â‰ˆ 0.068 â†’ in band.
    flavors = _compute_signal_flavors(0.70, 100, 70)
    assert flavors["p_B_50"] == pytest.approx(0.70, abs=1e-6)
    sig = _eval(
        STRATEGY_CONFIGS["YHIGH"], "YHIGH",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_ceiling_market(best_ask=0.62, best_bid=0.36),
        threshold_val=30.0, bracket_low=30.0, bracket_high=None,
        bracket_kind="ceiling",
        bracket_label_str="â‰¥30Â°C", bracket_unit="F",
        p_emos=0.70, pred_bucket=(0.65, 0.75), flavors=flavors,
        n_cum=100, capital=10000.0,
        local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.strategy == "YHIGH"
    assert sig.signal_used == "p_B_50"
    assert sig.passed_all_gates


def test_yhigh_skips_on_interior_bracket(conn: sqlite3.Connection) -> None:
    # YHIGH only fires on ceiling brackets; interior must return None silently.
    flavors = _compute_signal_flavors(0.70, 100, 70)
    sig = _eval(
        STRATEGY_CONFIGS["YHIGH"], "YHIGH",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.62, best_bid=0.36),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22Â°C", bracket_unit="F",
        p_emos=0.70, pred_bucket=(0.65, 0.75), flavors=flavors,
        n_cum=100, capital=10000.0,
        local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is None


def test_yhigh_skips_below_fp_floor(conn: sqlite3.Connection) -> None:
    # YHIGH requires yes_price â‰¥ 0.50.
    flavors = _compute_signal_flavors(0.70, 100, 70)
    sig = _eval(
        STRATEGY_CONFIGS["YHIGH"], "YHIGH",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_ceiling_market(best_ask=0.40, best_bid=0.58),
        threshold_val=30.0, bracket_low=30.0, bracket_high=None,
        bracket_kind="ceiling",
        bracket_label_str="â‰¥30Â°C", bracket_unit="F",
        p_emos=0.70, pred_bucket=(0.65, 0.75), flavors=flavors,
        n_cum=100, capital=10000.0,
        local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    # Below fp band â†’ None (strategy doesn't apply).
    assert sig is None


def test_yhigh_cold_start_emits_skip_signal(conn: sqlite3.Connection) -> None:
    # n_cum=5 < LUT_MIN_N_FOR_SHRINKAGE=30 â†’ cold-start branch fires:
    # emits a SKIP BetSignal with lut_min_n=False, edge_gate=False so the
    # dashboard records the rejection (parallel to the NO/YMID/TAIL pattern).
    flavors = _compute_signal_flavors(0.70, 5, 4)
    sig = _eval(
        STRATEGY_CONFIGS["YHIGH"], "YHIGH",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_ceiling_market(best_ask=0.62, best_bid=0.36),
        threshold_val=30.0, bracket_low=30.0, bracket_high=None,
        bracket_kind="ceiling",
        bracket_label_str="â‰¥30Â°C", bracket_unit="F",
        p_emos=0.70, pred_bucket=(0.65, 0.75), flavors=flavors,
        n_cum=5, capital=10000.0,
        local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.strategy == "YHIGH"
    assert sig.signal_used == "p_B_50"
    assert not sig.passed_all_gates
    assert sig.gate_results["lut_min_n"] is False
    assert sig.gate_results["edge_gate"] is False


# ------------------------ P2 #7: NO post-walk relaxed-vs-strict ceiling -----

def test_no_post_walk_strict_ceiling_at_0_15(conn: sqlite3.Connection) -> None:
    # NO strict path on a ceiling bracket: walked_edge must respect
    # cfg.max_edge=0.15, NOT max_edge_for_ceiling=0.35. We construct a
    # scenario where the strict path passes pre-walk (edge=0.10) and the
    # walker doesn't push it any higher â€” passes. A symmetric scenario at
    # edge>0.15 is unreachable at runtime because the pre-walk edge_pass
    # already excludes edge>0.15 on the strict path; this test pins the
    # strict-vs-extension distinction by asserting bracket_extension stays False
    # so the post-walk check would correctly use 0.15 if walker drift ever
    # reintroduced an edge above the strict ceiling.
    flavors = _compute_signal_flavors(0.11, 100, 50)
    sig = _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_ceiling_market(best_ask=0.20, best_bid=0.75),
        threshold_val=30.0, bracket_low=30.0, bracket_high=None,
        bracket_kind="ceiling",
        bracket_label_str="â‰¥30Â°C", bracket_unit="F",
        p_emos=0.11, pred_bucket=(0.10, 0.15), flavors=flavors,
        n_cum=100, capital=10000.0, local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.passed_all_gates
    assert sig.gate_results["bracket_extension"] is False
    # Implicit: post-walk uses max_edge=0.15 (strict ceiling), since
    # bracket_extension is False.


def test_no_post_walk_relaxed_ceiling_at_0_35_when_extension_fired(conn: sqlite3.Connection) -> None:
    # NO ceiling-extension path: walked_edge in (0.15, 0.35] should pass
    # because the post-walk ceiling reads max_edge_for_ceiling=0.35 when
    # bracket_extension is True. Edge of ~0.258 (above 0.15, below 0.35) is
    # constructed by p_B_50=0.85, no_price=0.60.
    # n=100, hits=80 â†’ p_L_obs=0.80. p_emos=0.90 â†’ p_B_50=0.5*0.90+0.5*0.80=0.85.
    # 1-p_B_50 = 0.15. edge_ext = 0.15 - 0.60 - fee... that's negative.
    # Re-derive: we want 1-p_B_50 high enough that extension fires.
    # p_emos=0.12 with n=100, hits=24 gives p_L_obs=0.24 and p_B_50=0.18.
    # 1-p_B_50=0.82. no_price=0.55: edge_ext = 0.82 - 0.55 - 0.05*0.55*0.45
    #   ~= 0.258, in (0.15, 0.35]. Strict fails because no_price<0.75.
    flavors = _compute_signal_flavors(0.12, 100, 24)
    assert flavors["p_B_50"] == pytest.approx(0.18, abs=1e-6)
    sig = _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_ceiling_market(best_ask=0.45, best_bid=0.55),
        threshold_val=30.0, bracket_low=30.0, bracket_high=None,
        bracket_kind="ceiling",
        bracket_label_str="â‰¥30Â°C", bracket_unit="F",
        p_emos=0.12, pred_bucket=(0.10, 0.15), flavors=flavors,
        n_cum=100, capital=10000.0, local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.passed_all_gates
    assert sig.gate_results["bracket_extension"] is True
    assert sig.signal_used == "p_B_50"
    # walked_edge ~= 0.258, which is above strict cfg.max_edge=0.15 but below
    # the extension's max_edge_for_ceiling=0.35. Pass demonstrates the
    # ceiling correctly used 0.35 not 0.15.
    assert 0.15 < sig.edge <= 0.35


def test_no_ceiling_extension_both_gates_fail(conn: sqlite3.Connection) -> None:
    # Strict fails (no_price=0.55<0.70) AND extension fails (edge_ext below the
    # min_edge=0.090 floor). The original scenario used p_emos=0.50 to inflate
    # p_B_50; that is now intercepted by the LUT-bucket filter (>= 0.40), so
    # we reconstruct the both-gates-fail case at p_emos=0.35 instead.
    # n=100, hits=50 -> p_L_obs=0.50 -> p_B_50=0.5*0.35+0.5*0.50=0.425.
    # 1-p_B_50=0.575. no_price=0.55 -> edge_ext = 0.575 - 0.55 - fee(0.55)
    # ~= 0.013, below the 0.090 floor -> extension fails.
    flavors = _compute_signal_flavors(0.35, 100, 50)
    sig = _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_ceiling_market(best_ask=0.45, best_bid=0.55),
        threshold_val=30.0, bracket_low=30.0, bracket_high=None,
        bracket_kind="ceiling",
        bracket_label_str="ge30C", bracket_unit="F",
        p_emos=0.35, pred_bucket=(0.25, 0.40), flavors=flavors,
        n_cum=100, capital=10000.0, local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.strategy == "NO"
    assert not sig.passed_all_gates
    assert sig.gate_results["edge_gate"] is False
    assert sig.gate_results["bracket_extension"] is False


# ------------------------------ NO bracket-conditional ceiling extension

def test_no_strict_path_still_fires_on_ceiling(conn: sqlite3.Connection) -> None:
    # When the strict NO gate (np>=0.75, edge in [0.090, 0.15]) passes on a
    # ceiling bracket, the extension path must NOT fire â€” bracket_extension
    # gate_result records False.
    flavors = _compute_signal_flavors(0.11, 100, 50)
    sig = _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_ceiling_market(best_ask=0.20, best_bid=0.75),
        threshold_val=30.0, bracket_low=30.0, bracket_high=None,
        bracket_kind="ceiling",
        bracket_label_str="â‰¥30Â°C", bracket_unit="F",
        p_emos=0.11, pred_bucket=(0.10, 0.15), flavors=flavors,
        n_cum=100, capital=10000.0, local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.strategy == "NO"
    assert sig.signal_used == "p_E"
    assert sig.passed_all_gates
    assert sig.gate_results.get("bracket_extension") is False


def test_no_ceiling_extension_fires_when_strict_misses(conn: sqlite3.Connection) -> None:
    # Strict gate fails (np_p=0.55 below the 0.75 floor) but ceiling
    # extension allows np_p>=0.50 with p_B_50 + edge in [0.090, 0.35].
    # Choose p_emos so p_B_50 lands in the extension band.
    # n=100, hits=30 â†’ p_L_obs=0.30; p_B_50 = 0.5*p_E + 0.5*0.30.
    # With p_E=0.20 â†’ p_B_50=0.25 â†’ 1-p_B_50=0.75.
    # no_price=0.55 gives edge_ext â‰ˆ 0.1876, inside the optimized band.
    flavors = _compute_signal_flavors(0.20, 100, 30)
    assert flavors["p_B_50"] == pytest.approx(0.25, abs=1e-6)
    sig = _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_ceiling_market(best_ask=0.45, best_bid=0.55),
        threshold_val=30.0, bracket_low=30.0, bracket_high=None,
        bracket_kind="ceiling",
        bracket_label_str="â‰¥30Â°C", bracket_unit="F",
        p_emos=0.20, pred_bucket=(0.15, 0.25), flavors=flavors,
        n_cum=100, capital=10000.0, local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert sig.strategy == "NO"
    assert sig.signal_used == "p_B_50"  # extension path used p_B_50, not p_E
    assert sig.passed_all_gates
    assert sig.gate_results.get("bracket_extension") is True


def test_no_ceiling_extension_does_not_fire_on_interior(conn: sqlite3.Connection) -> None:
    # Same numerics that pass extension on a ceiling bracket should NOT
    # produce a passing signal on an interior bracket â€” the relaxed fp
    # floor (0.50) is only opened on ceiling brackets, so the strict
    # 0.75 floor still applies on interiors and the strategy returns
    # None at the fp_band gate.
    flavors = _compute_signal_flavors(0.30, 100, 50)
    sig = _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id="KJFK",
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.45, best_bid=0.55),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22Â°C", bracket_unit="F",
        p_emos=0.30, pred_bucket=(0.25, 0.35), flavors=flavors,
        n_cum=100, capital=10000.0, local_now_hour=0,
        conn=conn, ledger_event_types=("bet",),
    )
    # Below-fp-floor on interior â†’ strategy doesn't apply, returns None.
    assert sig is None


# ---------------------------------------------- Hourly-first-tick gate (2026-05-16)
#
# Restricts OPENING a new slot to the first scheduler tick of each
# station's local hour. ``tick_index_for(icao, minute)`` uses an
# offset-aware computation: a tick scheduled at minute :09 that executes
# at :10 (1-min misfire) is still tick 0, so a worker queue backlog
# doesn't silently halt a station for the whole hour.

_GATE_STATION = "KJFK"
_GATE_OFFSET = icao_tick_offset(_GATE_STATION)


def _no_passing_evaluate(
    conn: sqlite3.Connection,
    *,
    local_now_minute: int | None,
) -> object:
    """Run ``_evaluate_strategy`` for NO on a bracket that would otherwise pass."""
    p_emos = 0.11
    flavors = _compute_signal_flavors(p_emos, 50, 10)
    return _eval(
        STRATEGY_CONFIGS["NO"], "NO",
        station_id=_GATE_STATION,
        target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.20, best_bid=0.75),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior",
        bracket_label_str="20-22Â°C", bracket_unit="F",
        p_emos=p_emos, pred_bucket=(0.10, 0.15), flavors=flavors,
        n_cum=50, capital=10000.0, local_now_hour=0,
        local_now_minute=local_now_minute,
        conn=conn, ledger_event_types=("bet",),
    )


def _insert_gate_slot_row(
    conn: sqlite3.Connection,
    *,
    bet_size: float,
    entry_top_price: float = 0.70,
) -> None:
    """Seed one PENDING NO row so slot_filled_usd > 0 (top-up path)."""
    detail = {"strategy": "NO", "bracket_low": 20.0, "entry_top_price": entry_top_price}
    conn.execute(
        """INSERT INTO ledger (bet_ts, station_id, target_date, threshold, side,
            bet_size, event_type, event_detail, outcome) VALUES (?,?,?,?,?,?,?,?,?)""",
        ("2026-05-10 10:00:00", _GATE_STATION, "2026-05-10", 22.0, "NO",
         bet_size, "bet", json.dumps(detail), "PENDING"),
    )
    conn.commit()


def test_tick_gate_skipped_when_local_now_minute_is_none(conn: sqlite3.Connection) -> None:
    """Back-compat: tests/replay callers omit local_now_minute, gate is skipped."""
    sig = _no_passing_evaluate(conn, local_now_minute=None)
    assert sig is not None
    assert sig.passed_all_gates


def test_tick_0_opens_new_slot_at_offset_minute(conn: sqlite3.Connection) -> None:
    """At the station's offset minute (tick 0), a fresh open passes."""
    sig = _no_passing_evaluate(conn, local_now_minute=_GATE_OFFSET)
    assert sig is not None
    assert sig.passed_all_gates


def test_tick_0_opens_at_last_minute_of_decile(conn: sqlite3.Connection) -> None:
    """Boundary: the last minute of tick 0 (offset + SCAN_INTERVAL - 1) still opens."""
    minute = (_GATE_OFFSET + SCAN_INTERVAL_MINUTES - 1) % 60
    sig = _no_passing_evaluate(conn, local_now_minute=minute)
    assert sig is not None
    assert sig.passed_all_gates


def test_tick_0_opens_under_one_minute_misfire(conn: sqlite3.Connection) -> None:
    """The misfire-resilience fix: a tick scheduled at offset firing 1 min late (offset
    + 1) still reads as tick 0 thanks to the offset-aware computation."""
    minute = (_GATE_OFFSET + 1) % 60
    sig = _no_passing_evaluate(conn, local_now_minute=minute)
    assert sig is not None
    assert sig.passed_all_gates


def test_tick_1_blocks_new_open(conn: sqlite3.Connection) -> None:
    """Tick 1 (first minute of the second decile) blocks fresh opens — returns None."""
    minute = (_GATE_OFFSET + SCAN_INTERVAL_MINUTES) % 60
    sig = _no_passing_evaluate(conn, local_now_minute=minute)
    assert sig is None


def test_tick_1_blocks_new_open_under_misfire(conn: sqlite3.Connection) -> None:
    """A tick scheduled at offset+SCAN_INTERVAL firing 1 min late still reads as tick 1
    — the gate continues to block."""
    minute = (_GATE_OFFSET + SCAN_INTERVAL_MINUTES + 1) % 60
    sig = _no_passing_evaluate(conn, local_now_minute=minute)
    assert sig is None


def test_tick_1_allows_topup_when_slot_filled(conn: sqlite3.Connection) -> None:
    """Top-up path: when slot_filled > 0 (a prior tick partially filled), the gate does
    NOT block on tick 1+."""
    _insert_gate_slot_row(conn, bet_size=200.0)
    minute = (_GATE_OFFSET + SCAN_INTERVAL_MINUTES) % 60  # tick 1
    sig = _no_passing_evaluate(conn, local_now_minute=minute)
    assert sig is not None
    assert sig.passed_all_gates
    assert sig.slot_filled_pre == 200.0




# ------------------ Removed L2 ask-premium gate; true depth owns fill quality

def test_dislocated_book_can_trade_when_realized_edge_holds(conn: sqlite3.Connection) -> None:
    """A book ask above the old +10c premium guard can pass if VWAP edge holds."""
    flavors = {k: 0.25 for k in ("p_E", "p_B_50", "p_L_loose", "p_Shrink_n10", "tail_vote_avg")}
    mkt = {
        "best_ask": 0.02, "best_bid": 0.98, "volume24hr": 1000.0,
        "market_id": "0xtail", "token_id": "TOK_YES", "no_token_id": "TOK_NO",
        "bracket_low": 20.0, "bracket_high": 22.0, "bracket_label": "20-22C",
        # Old premium guard would reject 0.13 - 0.02 > 0.10. The edge walker
        # now decides from executable VWAP quality instead.
        "_yes_book": {"asks": [{"price": "0.13", "size": "100000"}],
                      "bids": [{"price": "0.51", "size": "100000"}]},
        "_no_book": {"asks": [{"price": "0.80", "size": "100000"}],
                     "bids": [{"price": "0.79", "size": "100000"}]},
    }
    sig = _eval(
        STRATEGY_CONFIGS["TAIL"], "TAIL",
        station_id="KJFK", target_date="2026-05-10", horizon=1, bi=0,
        mkt=mkt, threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior", bracket_label_str="20-22C", bracket_unit="F",
        p_emos=0.25, pred_bucket=(0.25, 0.40), flavors=flavors,
        n_cum=50, capital=10000.0, local_now_hour=1,
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is not None
    assert "l2_ask_premium" not in sig.gate_results
    assert sig.passed_all_gates

def test_tail_skipped_at_disallowed_hour_zero(conn: sqlite3.Connection) -> None:
    """After the L2 champion moved TAIL to local hour {1}, an otherwise-valid TAIL
    candidate at local hour 0 must hard-skip via the hour gate (-> None), not silently
    slip through."""
    flavors = _compute_signal_flavors(0.25, 50, 13)
    sig = _eval(
        STRATEGY_CONFIGS["TAIL"], "TAIL",
        station_id="KJFK", target_date="2026-05-10", horizon=1, bi=0,
        mkt=_bracket_market(best_ask=0.02, best_bid=0.94, volume=1000,
                            yes_ask_levels=[{"price": "0.02", "size": "10000"}]),
        threshold_val=22.0, bracket_low=20.0, bracket_high=22.0,
        bracket_kind="interior", bracket_label_str="20-22C", bracket_unit="F",
        p_emos=0.25, pred_bucket=(0.25, 0.40), flavors=flavors,
        n_cum=50, capital=10000.0, local_now_hour=0,   # disallowed for TAIL now
        conn=conn, ledger_event_types=("bet",),
    )
    assert sig is None


# --------------- FLIP mirrors the NO gate (operator invariant, 2026-08-09)
#
# The operator's hard invariant for the FLIP sleeve: FLIP fires on exactly
# the brackets where the champion NO gate would fire (given FLIP's one extra
# precondition — a sane mirrored NO book price). Both branches now route
# through the shared ``_evaluate_no_gate``; these tests pin the invariant at
# the branch level so any future edit that re-forks the gate logic (or skews
# the shared numbers) fails loudly.


def _reliability_variants() -> list[tuple[str, ReliabilityProvider | None]]:
    """Calibration off, a shrinking isotonic curve, and a market-aware blend."""
    iso = ReliabilityProvider({
        GROUP_NO_F: ReliabilityCurve(breakpoints=[0.0, 1.0], values=[0.0, 0.95]),
    })
    blend = ReliabilityProvider({
        GROUP_NO_F: ReliabilityCurve(
            curve_type=CURVE_TYPE_BLEND, coefs=[0.10, 0.90, 0.05],
        ),
    })
    return [("cal_off", None), ("cal_isotonic", iso), ("cal_blend", blend)]


def test_flip_fires_iff_no_fires_across_gate_grid() -> None:
    """Grid sweep: FLIP's gate decision + shared numbers match NO exactly."""
    no_cfg = STRATEGY_CONFIGS["NO"]
    flip_cfg = STRATEGY_CONFIGS["FLIP"]
    prices = [0.40, 0.49, 0.50, 0.55, 0.70, 0.74, 0.75, 0.76, 0.80, 0.85, 0.90, 0.97]
    p_grid = [0.02, 0.05, 0.08, 0.11, 0.14, 0.20, 0.35]
    hits_grid = [10, 30, 50]

    outcomes: set[tuple[bool, bool]] = set()
    for label, rel in _reliability_variants():
        for bracket_unit in ("F", "C", ""):
            for bracket_kind in ("interior", "ceiling"):
                for p_emos in p_grid:
                    for hits in hits_grid:
                        flavors = _compute_signal_flavors(p_emos, 100, hits)
                        for price in prices:
                            no_res = _evaluate_no_branch(
                                no_cfg, fill_price=price, flavors=flavors,
                                bracket_kind=bracket_kind, cold_start=False,
                                n_cum=100, reliability=rel,
                                bracket_unit=bracket_unit, no_price=price,
                            )
                            flip_res = _evaluate_flip_branch(
                                flip_cfg, fill_price=0.20, flavors=flavors,
                                bracket_kind=bracket_kind, cold_start=False,
                                n_cum=100, reliability=rel,
                                bracket_unit=bracket_unit, no_price=price,
                            )
                            ctx = (
                                f"{label} unit={bracket_unit!r} kind={bracket_kind} "
                                f"p={p_emos} hits={hits} price={price}"
                            )
                            assert no_res is not None and flip_res is not None, ctx
                            # THE invariant: FLIP fires iff NO fires.
                            assert flip_res.edge_pass == no_res.edge_pass, ctx
                            # Shared gate numbers are bit-identical.
                            assert flip_res.edge == no_res.edge, ctx
                            assert (
                                flip_res.partial_gates["claimed_raw"]
                                == no_res.partial_gates["claimed_raw"]
                            ), ctx
                            assert (
                                flip_res.partial_gates["claimed_calibrated"]
                                == no_res.partial_gates["claimed_calibrated"]
                            ), ctx
                            # Extension fires (or not) identically. FLIP omits
                            # the key when the extension didn't fire; the
                            # merged gate_results seed it to False upstream.
                            no_ext = no_res.partial_gates["bracket_extension"]
                            flip_ext = flip_res.partial_gates.get(
                                "bracket_extension", False
                            )
                            assert flip_ext == no_ext, ctx
                            # Intended inversions: P(YES) floor, _flip naming,
                            # walker floor -1.0 (accept any negative edge).
                            assert flip_res.prob_safe_floor == pytest.approx(
                                1.0 - no_res.prob_safe_floor, abs=1e-12
                            ), ctx
                            assert (
                                flip_res.signal_used
                                == f"{no_res.signal_used}_flip"
                            ), ctx
                            assert flip_res.signal_value == no_res.signal_value, ctx
                            assert flip_res.edge_min_for_walker == -1.0, ctx
                            outcomes.add((no_res.edge_pass, bool(no_ext)))

    # The grid must actually visit every reachable gate outcome — fail,
    # strict pass, extension pass — or the sweep silently degenerated.
    # (extension_fired=True implies edge_pass, so (False, True) is unreachable.)
    assert outcomes == {(False, False), (True, False), (True, True)}


def test_flip_requires_valid_no_price_while_no_does_not() -> None:
    """FLIP's single extra precondition: mirrored NO book price in (0, 1)."""
    flavors = _compute_signal_flavors(0.11, 100, 30)
    for bad in (None, 0.0, 1.0, 1.5, -0.2):
        flip_res = _evaluate_flip_branch(
            STRATEGY_CONFIGS["FLIP"], fill_price=0.20, flavors=flavors,
            bracket_kind="interior", cold_start=False, n_cum=100,
            reliability=None, bracket_unit="F", no_price=bad,
        )
        assert flip_res is None, f"no_price={bad!r}"
    no_res = _evaluate_no_branch(
        STRATEGY_CONFIGS["NO"], fill_price=0.75, flavors=flavors,
        bracket_kind="interior", cold_start=False, n_cum=100,
        reliability=None, bracket_unit="F", no_price=None,
    )
    assert no_res is not None
    assert no_res.edge_pass


def test_flip_cold_start_skips_like_no() -> None:
    """Cold start (n_cum below LUT floor) produces a non-firing SKIP on both
    branches, with FLIP keeping its inverted P(YES) floor and _flip naming."""
    flavors = _compute_signal_flavors(0.11, 5, 1)
    no_res = _evaluate_no_branch(
        STRATEGY_CONFIGS["NO"], fill_price=0.75, flavors=flavors,
        bracket_kind="interior", cold_start=True, n_cum=5,
        reliability=None, bracket_unit="F", no_price=0.75,
    )
    flip_res = _evaluate_flip_branch(
        STRATEGY_CONFIGS["FLIP"], fill_price=0.20, flavors=flavors,
        bracket_kind="interior", cold_start=True, n_cum=5,
        reliability=None, bracket_unit="F", no_price=0.75,
    )
    assert no_res is not None and flip_res is not None
    assert no_res.edge_pass is False
    assert flip_res.edge_pass is False
    assert no_res.partial_gates["lut_min_n"] is False
    assert flip_res.partial_gates["lut_min_n"] is False
    assert no_res.signal_used == "p_E"
    assert flip_res.signal_used == "p_E_flip"
    assert flip_res.prob_safe_floor == pytest.approx(
        1.0 - no_res.prob_safe_floor, abs=1e-12
    )
    assert flip_res.edge_min_for_walker == -1.0


def test_flip_and_no_both_silent_skip_on_nan_p_e() -> None:
    """NaN p_E means neither branch can evaluate — both return None."""
    flavors = {
        k: float("nan")
        for k in ("p_E", "p_B_50", "p_L_loose", "p_Shrink_n10", "p_Shrink_n50")
    }
    assert _evaluate_no_branch(
        STRATEGY_CONFIGS["NO"], fill_price=0.75, flavors=flavors,
        bracket_kind="interior", cold_start=False, n_cum=100,
        reliability=None, bracket_unit="F", no_price=0.75,
    ) is None
    assert _evaluate_flip_branch(
        STRATEGY_CONFIGS["FLIP"], fill_price=0.20, flavors=flavors,
        bracket_kind="interior", cold_start=False, n_cum=100,
        reliability=None, bracket_unit="F", no_price=0.75,
    ) is None
