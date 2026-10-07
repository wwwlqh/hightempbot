"""Compare local and server .env live-critical values safely.

Examples:
    python -m hightempbot.cli.env_parity --server-env-file server.env
    python -m hightempbot.cli.env_parity --ssh-target opc@<server-ip> --ssh-key ssh-key-2026-03-20.key
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hightempbot.execution.live_readiness import (
    CRITICAL_ENV_KEYS,
    build_env_parity_report,
    read_server_env_via_ssh,
)


def _print_human(report) -> None:
    print(f"Env parity: {report.status}")
    print(f"Compared keys: {', '.join(report.compared_keys)}")
    if report.missing_local:
        print(f"Missing locally: {', '.join(report.missing_local)}")
    if report.missing_server:
        print(f"Missing on server: {', '.join(report.missing_server)}")
    if report.mismatches:
        print("Mismatches:")
        for item in report.mismatches:
            print(
                "  "
                f"{item['key']}: local={item['localFingerprint']} "
                f"server={item['serverFingerprint']}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--local-env", default=".env", help="Local .env path.")
    parser.add_argument(
        "--server-env-file",
        help="Already-downloaded server .env file to compare against.",
    )
    parser.add_argument("--ssh-target", help="SSH target, for example opc@host.")
    parser.add_argument("--ssh-key", help="SSH private key for --ssh-target.")
    parser.add_argument(
        "--server-env-path",
        default="~/hightempbot/.env",
        help="Remote .env path when using --ssh-target.",
    )
    parser.add_argument(
        "--key",
        action="append",
        dest="keys",
        help="Restrict comparison to this key. Repeatable.",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="Ignore this key. Repeatable.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument(
        "--all-keys",
        action="store_true",
        help=(
            "Compare the FULL union of local + server env, not just the "
            "live-critical allowlist. Noisier (flags any key drift) but "
            "catches server-only env additions that the allowlist misses. "
            "Default behavior compares CRITICAL_ENV_KEYS only — the trade-off "
            "is documented in execution/live_readiness.py."
        ),
    )
    args = parser.parse_args(argv)

    if args.server_env_file:
        server_env_text = Path(args.server_env_file).read_text(encoding="utf-8")
    elif args.ssh_target:
        if not args.ssh_key:
            raise SystemExit("--ssh-key is required with --ssh-target")
        server_env_text = read_server_env_via_ssh(
            ssh_target=args.ssh_target,
            ssh_key=args.ssh_key,
            server_env_path=args.server_env_path,
        )
    else:
        raise SystemExit("Provide --server-env-file or --ssh-target/--ssh-key.")

    # --all-keys overrides the live-critical allowlist
    # by taking the full union of local + server keys. --key (repeatable)
    # still takes precedence if the operator explicitly named keys.
    if args.keys:
        compare_keys = args.keys
    elif args.all_keys:
        from hightempbot.execution.live_readiness import (
            load_env_file,
            parse_env_text,
        )
        local_env_map = load_env_file(args.local_env)
        server_env_map = parse_env_text(server_env_text)
        compare_keys = sorted(set(local_env_map.keys()) | set(server_env_map.keys()))
    else:
        compare_keys = CRITICAL_ENV_KEYS

    report = build_env_parity_report(
        local_env_path=args.local_env,
        server_env_text=server_env_text,
        keys=compare_keys,
        ignore=args.ignore,
    )

    if args.json:
        print(json.dumps(report.to_public_dict(), indent=2, sort_keys=True))
    else:
        _print_human(report)
    from hightempbot.cli._exitcodes import OK, NOGO

    return OK if report.ok else NOGO


if __name__ == "__main__":
    raise SystemExit(main())
