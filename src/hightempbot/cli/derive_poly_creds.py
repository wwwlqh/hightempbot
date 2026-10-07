"""Derive Polymarket CLOB API credentials from POLY_PRIVATE_KEY.

Polymarket signs CLOB orders with HMAC creds derived deterministically from
your wallet's L2 signer private key. You cannot copy these three values from
a settings page; they must be generated programmatically (see `.env.example`).

This CLI calls `py_clob_client_v2.ClobClient.create_or_derive_api_key` and
prints the three credentials in .env-paste format. Idempotent: the derivation
is deterministic for a given private key, so re-running on the same wallet
returns the same trio. Use this to bootstrap a wallet, verify the trio already
in `.env`, and probe which Polymarket wallet topology can see the pUSD balance.

Usage:
    python -m hightempbot.cli.derive_poly_creds
    python -m hightempbot.cli.derive_poly_creds --key 0x...
    python -m hightempbot.cli.derive_poly_creds --check
    python -m hightempbot.cli.derive_poly_creds --signature-type 3 --funder 0x... --sync-balance --probe-balance
    python -m hightempbot.cli.poly_wallet_diagnostics --check-relayer --probe-clob

Never echo the private key or the derived creds to a shared log. Stdout-only
by design.
"""

from __future__ import annotations

import argparse
import sys


# reuse the canonical wallet validator from
# execution.walker rather than maintain a parallel copy here. The CLI wraps the
# ValueError raised by the shared helper into SystemExit so user-facing exit
# behaviour is unchanged.
from hightempbot.execution.walker import (
    _SIGNATURE_TYPE_LABELS,
    _validate_wallet_config as _validate_wallet_config_shared,
)


def _validate_key(key: str) -> None:
    if not key or not key.startswith("0x") or len(key) < 66:
        raise SystemExit(
            "POLY_PRIVATE_KEY must start with 0x and be 64 hex chars (66 total). "
            "Got something shorter or not 0x-prefixed. Refusing to call CLOB with it."
        )


def _validate_wallet_config(
    signature_type: int | None,
    funder: str | None,
) -> tuple[int, str]:
    try:
        return _validate_wallet_config_shared(signature_type, funder)
    except ValueError as exc:
        # CLI surface: exit non-zero with the same message rather than a
        # raw Python traceback. Message text from the shared helper is
        # already operator-readable.
        raise SystemExit(str(exc)) from exc


def _client_kwargs(signature_type: int, funder: str) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if signature_type != 0:
        kwargs["signature_type"] = signature_type
    if funder:
        kwargs["funder"] = funder
    return kwargs


def _derive(
    key: str,
    *,
    signature_type: int = 0,
    funder: str = "",
) -> tuple[str, str, str]:
    """Return (api_key, api_secret, api_passphrase) for the given private key."""
    _validate_key(key)
    signature_type, funder = _validate_wallet_config(signature_type, funder)

    try:
        from py_clob_client_v2.client import ClobClient
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "py_clob_client_v2 is not installed - install the bot's deps "
            "(`pip install -e .[dev]`) before running this CLI."
        ) from exc

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=key,
        **_client_kwargs(signature_type, funder),
    )
    creds = client.create_or_derive_api_key()
    return creds.api_key, creds.api_secret, creds.api_passphrase


def _probe_balance(
    key: str,
    trio: tuple[str, str, str],
    *,
    signature_type: int,
    funder: str,
    sync_balance: bool = False,
) -> float:
    """Return pUSD collateral balance for the selected wallet topology."""
    _validate_key(key)
    signature_type, funder = _validate_wallet_config(signature_type, funder)

    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import (
            ApiCreds,
            AssetType,
            BalanceAllowanceParams,
        )
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "py_clob_client_v2 is not installed - install the bot's deps "
            "(`pip install -e .[dev]`) before running this CLI."
        ) from exc

    api_key, api_secret, api_passphrase = trio
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=key,
        creds=ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        ),
        **_client_kwargs(signature_type, funder),
    )
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    if sync_balance:
        client.update_balance_allowance(params)
    result = client.get_balance_allowance(params)
    try:
        return int(result.get("balance", "0")) / 1e6
    except (AttributeError, TypeError, ValueError) as exc:
        raise SystemExit(f"Unexpected CLOB balance response: {result!r}") from exc


def _print_probe_balance(
    key: str,
    trio: tuple[str, str, str],
    *,
    signature_type: int,
    funder: str,
    sync_balance: bool = False,
) -> None:
    balance = _probe_balance(
        key, trio, signature_type=signature_type, funder=funder,
        sync_balance=sync_balance,
    )
    label = _SIGNATURE_TYPE_LABELS[signature_type]
    funder_text = funder or "(auto-derived)"
    print()
    if sync_balance:
        print("CLOB balance allowance sync requested before probe.")
    print(
        f"pUSD balance for signature_type={signature_type} ({label}), "
        f"funder={funder_text}: ${balance:.2f}"
    )


def _warn_suspicious_topology(signature_type: int, funder: str) -> None:
    if signature_type == 1 and funder:
        print(
            "WARNING: POLY_SIGNATURE_TYPE=1 with POLY_FUNDER set. "
            "If this is a Polymarket deposit wallet, use "
            "POLY_SIGNATURE_TYPE=3 with the deposit wallet as POLY_FUNDER.",
            file=sys.stderr,
        )


def main() -> int:
    from hightempbot.cli._exitcodes import OK, NOGO

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--key",
        help=(
            "L2 signer private key (0x-prefixed, 64 hex). When omitted, "
            "read POLY_PRIVATE_KEY from the bot's runtime config."
        ),
    )
    parser.add_argument(
        "--check", action="store_true",
        help=(
            "Compare the derived trio against .env POLY_API_KEY / "
            "POLY_SECRET / POLY_PASSPHRASE. Does not modify .env."
        ),
    )
    parser.add_argument(
        "--signature-type",
        type=int,
        choices=sorted(_SIGNATURE_TYPE_LABELS),
        default=None,
        help=(
            "Polymarket wallet signature type: 0=EOA, 1=POLY_PROXY, "
            "2=POLY_GNOSIS_SAFE, 3=POLY_1271 deposit wallet. Defaults to "
            ".env POLY_SIGNATURE_TYPE when reading .env, else 0."
        ),
    )
    parser.add_argument(
        "--funder",
        default=None,
        help=(
            "Wallet address holding funds. Required for --signature-type 3 "
            "(POLY_1271 deposit wallets). Defaults to .env POLY_FUNDER "
            "when reading .env."
        ),
    )
    parser.add_argument(
        "--probe-balance",
        action="store_true",
        help=(
            "After deriving creds, call CLOB balance-allowance for the selected "
            "signature type/funder and print pUSD balance."
        ),
    )
    parser.add_argument(
        "--sync-balance",
        action="store_true",
        help=(
            "Call CLOB update_balance_allowance before probing. Implies "
            "--probe-balance and is required after deposit-wallet funding or "
            "allowance changes."
        ),
    )
    args = parser.parse_args()
    if args.sync_balance:
        args.probe_balance = True

    if args.key:
        key = args.key
        current_trio: tuple[str, str, str] | None = None
        signature_type = args.signature_type if args.signature_type is not None else 0
        funder = args.funder or ""
    else:
        try:
            from hightempbot.runtime_config import Config
        except ImportError as exc:
            raise SystemExit(
                "Could not import hightempbot.runtime_config - run from the "
                "project root with the bot's venv active."
            ) from exc
        cfg = Config()  # loads .env
        key = cfg.poly_private_key.get_secret_value()
        if not key:
            raise SystemExit(
                "POLY_PRIVATE_KEY is empty in your .env. Set it and re-run, "
                "or pass --key <0x...> explicitly."
            )
        current_trio = (
            cfg.poly_api_key.get_secret_value(),
            cfg.poly_secret.get_secret_value(),
            cfg.poly_passphrase.get_secret_value(),
        )
        signature_type = (
            args.signature_type
            if args.signature_type is not None
            else cfg.poly_signature_type
        )
        funder = args.funder if args.funder is not None else cfg.poly_funder

    signature_type, funder = _validate_wallet_config(signature_type, funder)
    _warn_suspicious_topology(signature_type, funder)

    api_key, api_secret, api_passphrase = _derive(
        key, signature_type=signature_type, funder=funder,
    )

    if args.check:
        if current_trio is None:
            raise SystemExit(
                "--check requires reading the existing trio from .env. "
                "Drop --key (or run without it) to compare."
            )
        cur_k, cur_s, cur_p = current_trio
        results = [
            ("POLY_API_KEY", cur_k, api_key),
            ("POLY_SECRET", cur_s, api_secret),
            ("POLY_PASSPHRASE", cur_p, api_passphrase),
        ]
        any_mismatch = False
        for name, cur, derived in results:
            if not cur:
                print(f"  {name}: MISSING in .env  (derived: {derived[:4]}...{derived[-4:]})")
                any_mismatch = True
            elif cur == derived:
                print(f"  {name}: OK")
            else:
                print(
                    f"  {name}: MISMATCH - .env has {cur[:4]}...{cur[-4:]} but "
                    f"derived is {derived[:4]}...{derived[-4:]}"
                )
                any_mismatch = True
        if any_mismatch:
            print()
            print(
                "One or more values differ. Update .env with the derived trio "
                "(re-run without --check to see the values)."
            )
            return NOGO
        print()
        print("All three values match the current POLY_PRIVATE_KEY.")
        if args.probe_balance:
            _print_probe_balance(
                key,
                (api_key, api_secret, api_passphrase),
                signature_type=signature_type,
                funder=funder,
                sync_balance=args.sync_balance,
            )
        return OK

    print("# Derived from POLY_PRIVATE_KEY (deterministic; same key -> same trio).")
    print("# Paste these lines into .env. Re-deploy by restarting the bot.")
    print(
        f"# Wallet topology: POLY_SIGNATURE_TYPE={signature_type} "
        f"({_SIGNATURE_TYPE_LABELS[signature_type]}), "
        f"POLY_FUNDER={funder or '(blank/auto-derived)'}"
    )
    print(f"POLY_API_KEY={api_key}")
    print(f"POLY_SECRET={api_secret}")
    print(f"POLY_PASSPHRASE={api_passphrase}")
    print(f"POLY_SIGNATURE_TYPE={signature_type}")
    print(f"POLY_FUNDER={funder}")
    if args.probe_balance:
        _print_probe_balance(
            key,
            (api_key, api_secret, api_passphrase),
            signature_type=signature_type,
            funder=funder,
            sync_balance=args.sync_balance,
        )
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
