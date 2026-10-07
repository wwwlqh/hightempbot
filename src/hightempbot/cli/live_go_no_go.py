"""Read-only live GO/NO-GO report (never places orders or moves funds)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hightempbot.db.connection import get_connection
from hightempbot.execution.live_readiness import (
    build_env_parity_report,
    build_live_readiness_report,
    read_server_env_via_ssh,
)
from hightempbot.persistence.wallet_reconciliation import wallet_dashboard_payload
from hightempbot.runtime_config import Config


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _live_fill_confirmed(conn) -> bool:
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM ledger
            WHERE event_type='bet'
              AND outcome IN ('PENDING', 'WIN', 'LOSS', 'CLOSED')
              AND transaction_hash IS NOT NULL
              AND transaction_hash NOT LIKE 'DRY_RUN_%'
            LIMIT 1
            """
        ).fetchone()
    except Exception:
        return False
    return row is not None


def _confidence_state(checks: list[dict[str, object]]) -> str:
    status_by_name = {str(c["name"]): str(c["status"]) for c in checks}
    if status_by_name.get("live_confirmed") == "OK":
        return "live-confirmed"
    if (
        status_by_name.get("env_parity") in {"OK", "SKIPPED"}
        and status_by_name.get("live_readiness") == "OK"
        and status_by_name.get("wallet_snapshot") == "OK"
    ):
        return "live-capable"
    if (
        status_by_name.get("env_parity") in {"OK", "SKIPPED"}
        and status_by_name.get("db_backup") in {"OK", "SKIPPED"}
        and status_by_name.get("schema") == "OK"
    ):
        return "server-ready"
    if status_by_name.get("schema") == "OK":
        return "code-ready"
    return "plan-ready"


def _print_human(payload: dict[str, object]) -> None:
    print(f"Confidence: {payload['confidence']}")
    print(f"GO: {payload['go']}")
    for check in payload["checks"]:
        print(f"- {check['status']} {check['name']}: {check['message']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--offline", action="store_true", help="Use DB/latest snapshots only; skip live CLOB/RPC checks.")
    parser.add_argument("--server-env-file")
    parser.add_argument("--ssh-target")
    parser.add_argument("--ssh-key")
    parser.add_argument("--server-env-path", default="~/hightempbot/.env")
    parser.add_argument("--require-db-backup", action="store_true")
    parser.add_argument("--db-backup")
    args = parser.parse_args(argv)

    cfg = Config()
    checks: list[dict[str, object]] = []

    env_report = None
    if args.server_env_file or args.ssh_target:
        if args.server_env_file:
            server_text = Path(args.server_env_file).read_text(encoding="utf-8")
        else:
            if not args.ssh_key:
                raise SystemExit("--ssh-key is required with --ssh-target")
            server_text = read_server_env_via_ssh(
                ssh_target=args.ssh_target,
                ssh_key=args.ssh_key,
                server_env_path=args.server_env_path,
            )
        env_report = build_env_parity_report(server_env_text=server_text)
        checks.append({
            "name": "env_parity",
            "status": "OK" if env_report.ok else "ERROR",
            "message": "local/server live-critical .env keys match" if env_report.ok else "local/server .env drift detected",
            "detail": env_report.to_public_dict(),
        })
    else:
        checks.append({
            "name": "env_parity",
            "status": "SKIPPED",
            "message": "server .env not supplied",
        })

    conn = get_connection(cfg.db_path)
    try:
        required_tables = [
            "operator_control_state",
            "operator_control_events",
            "live_readiness_reports",
            "wallet_reconciliation_runs",
            "transfer_requests",
        ]
        missing_tables = [name for name in required_tables if not _table_exists(conn, name)]
        checks.append({
            "name": "schema",
            "status": "OK" if not missing_tables else "ERROR",
            "message": "operator/live schema present" if not missing_tables else f"missing tables: {', '.join(missing_tables)}",
        })

        if args.require_db_backup:
            backup_path = Path(args.db_backup or f"{cfg.db_path}.bak")
            checks.append({
                "name": "db_backup",
                "status": "OK" if backup_path.exists() else "ERROR",
                "message": f"backup found: {backup_path}" if backup_path.exists() else f"backup missing: {backup_path}",
            })
        else:
            checks.append({"name": "db_backup", "status": "SKIPPED", "message": "not required by CLI args"})

        if args.offline:
            checks.append({"name": "live_readiness", "status": "SKIPPED", "message": "offline mode"})
        else:
            readiness = build_live_readiness_report(cfg)
            checks.append({
                "name": "live_readiness",
                "status": "OK" if readiness.ok else "ERROR",
                "message": "read-only live readiness passed" if readiness.ok else "read-only live readiness failed",
                "detail": readiness.to_public_dict(),
            })

        wallet = wallet_dashboard_payload(conn, config=cfg, dry_run=cfg.dry_run)
        wallet_ok = bool(wallet.get("fresh")) and not wallet.get("warnings")
        checks.append({
            "name": "wallet_snapshot",
            "status": "OK" if wallet_ok or cfg.dry_run else "ERROR",
            "message": "fresh wallet snapshot available" if wallet_ok else "wallet snapshot missing/stale/degraded",
            "detail": wallet,
        })

        confirmed = _live_fill_confirmed(conn)
        checks.append({
            "name": "live_confirmed",
            "status": "OK" if confirmed else "SKIPPED",
            "message": "real live fill/close reconciled" if confirmed else "no reconciled live fill/close yet",
        })
    finally:
        conn.close()

    confidence = _confidence_state(checks)
    go = confidence in {"live-capable", "live-confirmed"}
    payload = {"confidence": confidence, "go": go, "checks": checks}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human(payload)
    from hightempbot.cli._exitcodes import OK, NOGO

    return OK if go else NOGO


if __name__ == "__main__":
    raise SystemExit(main())
