"""Operator CLI: read + safely mutate live operator state from the shell.

ce-code-review P1 #10: gives the operator a non-dashboard surface to inspect
state, halt processing, and execute return-transfers when the dashboard is
unavailable (lost SSH tunnel, browser failure, locked-out auth). All actions
go through the same primitives the dashboard uses
(``hightempbot.execution.operator_control`` /
``hightempbot.execution.polymarket_transfer``) so audit events and DRY_RUN
fuses are identical.

Subcommands:

* ``status`` — print the current operator state JSON (state, version, dry_run).
* ``stop`` — set state=STOPPED_PROCESSING (refuses new bets, holds existing).
* ``start`` — set state=LIVE (only when the process did NOT boot DRY_RUN).
* ``transfer-preview`` — read-only transfer eligibility + amount preview.
* ``transfer-lock`` — set state=TRANSFER_LOCK (required before submit).
* ``transfer-submit`` — execute the return transfer. Requires --amount,
  --confirmation (exact string from preview), and the explicit
  ``--i-have-read-the-confirmation`` flag to prevent accidental fund movement.
* ``orphan-list`` — list recovered CLOB fills needing manual bracket linkage.
* ``orphan-patch`` — attach station/date/token/bracket metadata to one orphan.

Every subcommand uses ``actor=f"cli:<subcommand>"`` so the audit log
distinguishes CLI actions from dashboard / agent actions.

Example:
    python -m hightempbot.cli.operator status --json
    python -m hightempbot.cli.operator stop --reason "manual halt"
    python -m hightempbot.cli.operator transfer-preview --amount 50
    python -m hightempbot.cli.operator transfer-lock --reason "weekly sweep"
    python -m hightempbot.cli.operator transfer-submit \\
        --amount 50 --confirmation "TRANSFER 50.000000 PUSD TO 0x..." \\
        --i-have-read-the-confirmation
    python -m hightempbot.cli.operator orphan-list
    python -m hightempbot.cli.operator orphan-patch --id 42 --station-id KDAL \\
        --target-date 2026-05-20 --market-id 0x... --token-id 123 \\
        --threshold 63.5 --bracket-low 63.5 --reason "linked from Polymarket"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from typing import Any

from hightempbot.cli._exitcodes import NOGO, OK, UNEXPECTED, USAGE
from hightempbot.db.connection import get_connection
from hightempbot.runtime_config import Config


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ICAO_RE = re.compile(r"^[A-Z][A-Z0-9]{3}$")


def _print_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str))
    sys.stdout.write("\n")


def _parse_icao(value: str) -> str:
    upper = value.upper()
    if not _ICAO_RE.match(upper):
        raise argparse.ArgumentTypeError(
            f"station id must be a 4-char ICAO, got {value!r}"
        )
    return upper


def _parse_target_date(value: str) -> str:
    if not _ISO_DATE_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"target date must be ISO YYYY-MM-DD, got {value!r}"
        )
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def _cmd_status(args: argparse.Namespace, config: Config) -> int:
    from hightempbot.execution.operator_control import public_operator_payload

    with get_connection(config.db_path) as conn:
        payload = public_operator_payload(conn, dry_run=config.dry_run)
    _print_json(payload)
    return OK


def _cmd_stop(args: argparse.Namespace, config: Config) -> int:
    from hightempbot.execution.operator_control import stop_processing

    with get_connection(config.db_path) as conn:
        state = stop_processing(
            conn,
            actor="cli:stop",
            reason=args.reason or "operator stop processing (cli)",
            boot_dry_run=config.dry_run,
        )
    _print_json(state.to_public_dict())
    return OK


def _cmd_start(args: argparse.Namespace, config: Config) -> int:
    from hightempbot.execution.live_action_guard import (
        LiveActionSafetyError,
        assert_fresh_live_action_context,
    )
    from hightempbot.execution.operator_control import (
        OperatorControlError,
        start_processing,
    )

    with get_connection(config.db_path) as conn:
        try:
            assert_fresh_live_action_context(
                conn,
                config=config,
                dry_run=config.dry_run,
            )
            state = start_processing(
                conn,
                actor="cli:start",
                reason=args.reason or "operator start processing (cli)",
                boot_dry_run=config.dry_run,
            )
        except (LiveActionSafetyError, OperatorControlError) as exc:
            sys.stderr.write(f"start refused: {exc}\n")
            return NOGO
    _print_json(state.to_public_dict())
    return OK


def _cmd_transfer_preview(args: argparse.Namespace, config: Config) -> int:
    from hightempbot.execution.polymarket_transfer import (
        TransferSafetyError,
        preview_return_transfer,
    )

    with get_connection(config.db_path) as conn:
        try:
            preview = preview_return_transfer(
                conn,
                config=config,
                amount=args.amount,
                to_wallet=args.to_wallet,
                dry_run=config.dry_run,
            )
        except TransferSafetyError as exc:
            sys.stderr.write(f"preview refused: {exc}\n")
            return NOGO
    _print_json(preview.to_dict())
    return OK if preview.ok else NOGO


def _cmd_transfer_lock(args: argparse.Namespace, config: Config) -> int:
    from hightempbot.execution.live_action_guard import (
        LiveActionSafetyError,
        assert_fresh_live_action_context,
    )
    from hightempbot.execution.operator_control import (
        OperatorControlError,
        enter_transfer_lock,
    )

    with get_connection(config.db_path) as conn:
        try:
            assert_fresh_live_action_context(
                conn,
                config=config,
                dry_run=config.dry_run,
                require_no_exposure=True,
            )
            state = enter_transfer_lock(
                conn,
                actor="cli:transfer-lock",
                reason=args.reason or "operator transfer lock (cli)",
                boot_dry_run=config.dry_run,
            )
        except (LiveActionSafetyError, OperatorControlError) as exc:
            sys.stderr.write(f"transfer-lock refused: {exc}\n")
            return NOGO
    _print_json(state.to_public_dict())
    return OK


def _cmd_transfer_submit(args: argparse.Namespace, config: Config) -> int:
    if not args.i_have_read_the_confirmation:
        sys.stderr.write(
            "transfer-submit requires --i-have-read-the-confirmation. "
            "This flag is an explicit safety acknowledgement — set only after "
            "you have visually verified the confirmation string from a fresh "
            "transfer-preview matches what you typed for --confirmation.\n"
        )
        return USAGE

    from hightempbot.execution.polymarket_transfer import (
        SUBMIT_FRESHNESS_TTL_S,
        TransferSafetyError,
        submit_return_transfer,
    )
    from hightempbot.execution.live_action_guard import (
        LiveActionSafetyError,
        assert_fresh_live_action_context,
    )

    with get_connection(config.db_path) as conn:
        try:
            assert_fresh_live_action_context(
                conn,
                config=config,
                dry_run=config.dry_run,
                require_no_exposure=True,
                freshness_ttl_s_override=SUBMIT_FRESHNESS_TTL_S,
            )
            response = submit_return_transfer(
                conn,
                config=config,
                amount=args.amount,
                confirmation=args.confirmation,
                actor="cli:transfer-submit",
                to_wallet=args.to_wallet,
                dry_run=config.dry_run,
            )
        except (LiveActionSafetyError, TransferSafetyError) as exc:
            sys.stderr.write(f"transfer-submit refused: {exc}\n")
            return NOGO
        except Exception as exc:  # noqa: BLE001 — surface unexpected fault
            sys.stderr.write(f"transfer-submit failed: {type(exc).__name__}: {exc}\n")
            return UNEXPECTED
    _print_json(response)
    return OK


def _cmd_orphan_list(args: argparse.Namespace, config: Config) -> int:
    from hightempbot.persistence.reconciliation import list_recovered_orphans

    with get_connection(config.db_path) as conn:
        rows = list_recovered_orphans(conn, pending_only=not args.all)
    _print_json({"ok": True, "count": len(rows), "orphans": rows})
    return OK


def _cmd_orphan_patch(args: argparse.Namespace, config: Config) -> int:
    from hightempbot.persistence.reconciliation import patch_recovered_orphan

    try:
        with get_connection(config.db_path) as conn:
            row = patch_recovered_orphan(
                conn,
                ledger_id=args.id,
                station_id=args.station_id,
                market_id=args.market_id,
                token_id=args.token_id,
                target_date=args.target_date,
                threshold=args.threshold,
                bracket_low=args.bracket_low,
                bracket_high=args.bracket_high,
                side=args.side,
                bracket_label=args.bracket_label or None,
                actor="cli:orphan-patch",
                reason=args.reason or "",
            )
    except ValueError as exc:
        sys.stderr.write(f"orphan-patch refused: {exc}\n")
        return NOGO
    _print_json({"ok": True, "orphan": row})
    return OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="htb-operator",
        description=(
            "Shell-side operator surface for HighTempBot live state. Mirrors "
            "the dashboard controls via the same primitives so audit + DRY_RUN "
            "fuses behave identically."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Print current operator state JSON.")
    p_status.set_defaults(func=_cmd_status)

    p_stop = sub.add_parser("stop", help="Set state=STOPPED_PROCESSING.")
    p_stop.add_argument("--reason", default="")
    p_stop.set_defaults(func=_cmd_stop)

    p_start = sub.add_parser("start", help="Set state=LIVE (refuses if DRY_RUN=True at boot).")
    p_start.add_argument("--reason", default="")
    p_start.set_defaults(func=_cmd_start)

    p_preview = sub.add_parser(
        "transfer-preview",
        help="Read-only return-transfer preview + eligibility check.",
    )
    p_preview.add_argument("--amount", required=True, help="Amount in pUSD (decimal).")
    p_preview.add_argument(
        "--to-wallet",
        default=None,
        help="Destination address (defaults to POLY_RETURN_WALLET). "
             "Anything else is rejected — the destination is pinned.",
    )
    p_preview.set_defaults(func=_cmd_transfer_preview)

    p_lock = sub.add_parser(
        "transfer-lock",
        help="Set state=TRANSFER_LOCK (required precondition for submit).",
    )
    p_lock.add_argument("--reason", default="")
    p_lock.set_defaults(func=_cmd_transfer_lock)

    p_submit = sub.add_parser(
        "transfer-submit",
        help="Execute a return transfer. Requires explicit confirmation flag.",
    )
    p_submit.add_argument("--amount", required=True, help="Amount in pUSD (decimal).")
    p_submit.add_argument(
        "--confirmation",
        required=True,
        help="Exact confirmation string from transfer-preview. Strict equality "
             "match — any drift triggers a TransferSafetyError.",
    )
    p_submit.add_argument(
        "--to-wallet",
        default=None,
        help="Destination address (defaults to POLY_RETURN_WALLET). "
             "Pinned to POLY_RETURN_WALLET; anything else is refused.",
    )
    p_submit.add_argument(
        "--i-have-read-the-confirmation",
        action="store_true",
        help="REQUIRED safety acknowledgement. Without this flag the CLI "
             "refuses to submit, preventing accidental fund movement from a "
             "stale shell history or a copy-paste mistake.",
    )
    p_submit.set_defaults(func=_cmd_transfer_submit)

    p_orphan_list = sub.add_parser(
        "orphan-list",
        help="List recovered-orphan ledger rows that need manual bracket linkage.",
    )
    p_orphan_list.add_argument(
        "--all",
        action="store_true",
        help="Include terminal recovered-orphan rows. Default only lists PENDING rows.",
    )
    p_orphan_list.set_defaults(func=_cmd_orphan_list)

    p_orphan_patch = sub.add_parser(
        "orphan-patch",
        help="Patch station/date/token/bracket metadata onto a recovered orphan.",
    )
    p_orphan_patch.add_argument("--id", type=int, required=True, help="Ledger row id.")
    p_orphan_patch.add_argument("--station-id", type=_parse_icao, required=True)
    p_orphan_patch.add_argument("--target-date", type=_parse_target_date, required=True)
    p_orphan_patch.add_argument("--market-id", required=True)
    p_orphan_patch.add_argument("--token-id", required=True)
    p_orphan_patch.add_argument("--threshold", type=float, required=True)
    p_orphan_patch.add_argument("--bracket-low", type=float, default=None)
    p_orphan_patch.add_argument("--bracket-high", type=float, default=None)
    p_orphan_patch.add_argument("--side", choices=("YES", "NO"), default=None)
    p_orphan_patch.add_argument("--bracket-label", default="")
    p_orphan_patch.add_argument("--reason", default="")
    p_orphan_patch.set_defaults(func=_cmd_orphan_patch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = Config()
    try:
        return int(args.func(args, config))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort surface
        sys.stderr.write(f"unexpected error: {type(exc).__name__}: {exc}\n")
        return UNEXPECTED


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
