"""Shared test fixtures and helpers.

ce-code-review P3 #70: ``seed_operator_live`` lets tests opt back into the
pre-#70 default of an immediately-LIVE operator state. ``db/schema.sql``
now seeds the singleton row as ``STOPPED_PROCESSING`` so a fresh install
boots in a safe halt; tests that exercise the betting pipeline / dashboard
under an enabled operator state should call this helper right after
``init_db`` to mirror the legacy fixture behavior.
"""

from __future__ import annotations

import os
import sqlite3

# Tests must never inherit live Telegram credentials from the local `.env`.
# Several integration-style tests exercise real notification call sites and
# rely on mocks only in the specific test under assertion.
os.environ["NOTIFY_TELEGRAM_TOKEN"] = ""
os.environ["NOTIFY_TELEGRAM_CHAT_ID"] = ""

from hightempbot.decision.strategies import (
    BracketContext,
    CalibrationContext,
    _evaluate_strategy as _evaluate_strategy_impl,
)


def eval_strategy(cfg, strategy_name, *, bi, mkt, threshold_val, bracket_low,
                  bracket_high, bracket_kind, bracket_label_str, bracket_unit,
                  p_emos, pred_bucket, flavors, n_cum,
                  **rest):
    """Bridge legacy flat-kwarg shape to the BracketContext + CalibrationContext signature.

    Tests predate the 2026-05-26 _evaluate_strategy signature change that
    bundled 8 bracket fields into BracketContext and 4 calibration fields
    into CalibrationContext. Keeping the flat kwargs at call sites lets
    each test's bracket parameters stay visible inline.
    """
    return _evaluate_strategy_impl(
        cfg, strategy_name,
        bracket=BracketContext(
            bracket_idx=bi, mkt=mkt, threshold_val=threshold_val,
            bracket_low=bracket_low, bracket_high=bracket_high,
            bracket_kind=bracket_kind, bracket_label=bracket_label_str,
            bracket_unit=bracket_unit,
        ),
        calibration=CalibrationContext(
            p_emos=p_emos, pred_bucket=pred_bucket,
            flavors=flavors, n_cum=n_cum,
        ),
        **rest,
    )


def seed_operator_live(conn: sqlite3.Connection, *, boot_dry_run: bool = False) -> None:
    """Force the operator_control singleton into the LIVE state.

    Idempotent: works whether the row already exists (UPSERT) or not.
    Boot_dry_run defaults to False so callers don't have to wrestle with
    the dry-run hard fuse — pass True if you specifically want to test
    the dry-run start-processing refusal.
    """
    conn.execute(
        """
        INSERT INTO operator_control_state
            (id, state, boot_dry_run, reason, updated_by)
        VALUES
            (1, 'LIVE', ?, 'test fixture: seeded LIVE', 'test')
        ON CONFLICT(id) DO UPDATE SET
            state='LIVE',
            boot_dry_run=excluded.boot_dry_run,
            reason='test fixture: seeded LIVE',
            updated_by='test',
            updated_at=datetime('now')
        """,
        (1 if boot_dry_run else 0,),
    )
    conn.commit()
