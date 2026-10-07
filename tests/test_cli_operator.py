"""Smoke tests for hightempbot.cli.operator.

ce-code-review P1 #10: verify every subcommand exposes a working --help
without requiring a configured DB. Mutation paths are intentionally not
exercised here — they need a live DB + relayer fixture, which lives in
the dashboard/integration test suites.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hightempbot.cli import operator
from hightempbot.cli._exitcodes import NOGO
from hightempbot.db.connection import init_db
from hightempbot.execution.live_action_guard import LiveActionSafetyError


def _cli_config(db_path, *, dry_run: bool = False) -> SimpleNamespace:
    return SimpleNamespace(db_path=str(db_path), dry_run=dry_run)


@pytest.mark.parametrize(
    "subcommand",
    [
        "status",
        "stop",
        "start",
        "transfer-preview",
        "transfer-lock",
        "transfer-submit",
    ],
)
def test_subcommand_help_succeeds(subcommand: str, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        operator.main([subcommand, "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert subcommand in out or "usage" in out.lower()


def test_top_level_help_succeeds(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        operator.main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    # Confirm all 6 subcommands appear in the top-level help.
    for sub in ("status", "stop", "start", "transfer-preview", "transfer-lock", "transfer-submit"):
        assert sub in out


def test_transfer_submit_requires_safety_flag(tmp_path, monkeypatch, capsys) -> None:
    """Missing --i-have-read-the-confirmation must refuse with USAGE exit."""
    # Point at an isolated DB so we don't touch the operator's real one.
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    rc = operator.main([
        "transfer-submit",
        "--amount", "1",
        "--confirmation", "TRANSFER 1 PUSD TO 0x0",
    ])
    # USAGE=1 per cli/_exitcodes.py — missing required safety flag.
    assert rc == 1
    err = capsys.readouterr().err
    assert "i-have-read-the-confirmation" in err


def test_start_uses_live_action_guard_before_mutating(tmp_path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "operator-start.db"
    init_db(db_path).close()
    calls: list[dict] = []

    def _guard(_conn, **kwargs):
        calls.append(kwargs)
        raise LiveActionSafetyError("readiness gate failed")

    monkeypatch.setattr(
        "hightempbot.execution.live_action_guard.assert_fresh_live_action_context",
        _guard,
    )

    rc = operator._cmd_start(
        SimpleNamespace(reason="test"),
        _cli_config(db_path),
    )

    assert rc == NOGO
    assert calls and calls[0]["dry_run"] is False
    assert calls[0].get("require_no_exposure") in (None, False)
    assert "readiness gate failed" in capsys.readouterr().err


def test_transfer_lock_uses_live_action_guard_with_exposure_check(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    db_path = tmp_path / "operator-transfer-lock.db"
    init_db(db_path).close()
    calls: list[dict] = []

    def _guard(_conn, **kwargs):
        calls.append(kwargs)
        raise LiveActionSafetyError("open orders exist")

    monkeypatch.setattr(
        "hightempbot.execution.live_action_guard.assert_fresh_live_action_context",
        _guard,
    )

    rc = operator._cmd_transfer_lock(
        SimpleNamespace(reason="test"),
        _cli_config(db_path),
    )

    assert rc == NOGO
    assert calls and calls[0]["require_no_exposure"] is True
    assert "open orders exist" in capsys.readouterr().err


def test_transfer_submit_uses_live_action_guard_before_submit(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    db_path = tmp_path / "operator-transfer-submit.db"
    init_db(db_path).close()
    calls: list[dict] = []

    def _guard(_conn, **kwargs):
        calls.append(kwargs)
        raise LiveActionSafetyError("fresh context required")

    monkeypatch.setattr(
        "hightempbot.execution.live_action_guard.assert_fresh_live_action_context",
        _guard,
    )

    rc = operator._cmd_transfer_submit(
        SimpleNamespace(
            amount="10",
            confirmation="TRANSFER 10.000000 PUSD TO 0x" + "e" * 40,
            i_have_read_the_confirmation=True,
            to_wallet=None,
        ),
        _cli_config(db_path),
    )

    assert rc == NOGO
    assert calls and calls[0]["require_no_exposure"] is True
    assert calls[0]["freshness_ttl_s_override"] == 30
    assert "fresh context required" in capsys.readouterr().err
