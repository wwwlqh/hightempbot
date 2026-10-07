"""Diagnose Polymarket proxy/safe/deposit-wallet readiness.

This is an operator GO/NO-GO tool for live trading. It keeps wallet topology
checks outside the betting loop so the bot can fail closed when Polymarket's
CLOB/relayer account state is not compatible with order placement.

Usage:
    python -m hightempbot.cli.poly_wallet_diagnostics
    python -m hightempbot.cli.poly_wallet_diagnostics --check-relayer
    python -m hightempbot.cli.poly_wallet_diagnostics --probe-clob
    python -m hightempbot.cli.poly_wallet_diagnostics --deploy-deposit-wallet --wait
"""

from __future__ import annotations

import argparse
import functools
import json
import time
from typing import Any

from hightempbot.polymarket.primitives import (
    CHAIN_ID,
    CONFIRMED_STATE,
    CTF_ADDRESS,
    CTF_COLLATERAL_ADAPTER_ADDRESS,
    CTF_EXCHANGE_ADDRESS,
    DEFAULT_RELAYER_URL,
    DEPLOYED_PATH,
    FAILED_STATES,
    NEG_RISK_ADAPTER_ADDRESS,
    NEG_RISK_CTF_COLLATERAL_ADAPTER_ADDRESS,
    NEG_RISK_CTF_EXCHANGE_ADDRESS,
    NONCE_PATH,
    PUSD_ADDRESS,
    RELAY_PAYLOAD_PATH,
    RELAYER_KEY_PATH,
    SUBMIT_PATH,
    TRANSACTION_PATH,
    WalletTopology,
)
from hightempbot.polymarket import primitives as _polymarket_primitives


def _to_systemexit(fn):
    """Adapt a primitive that raises ValueError/RuntimeError into SystemExit.

    The CLI's existing error model expects a single SystemExit per failure.
    Primitives moved to hightempbot.polymarket.primitives raise ValueError /
    RuntimeError so they're safe to call from production code; this adapter
    keeps the CLI's pre-existing surface unchanged.
    """
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc
    return _wrapped


def _mask(value: str, *, keep: int = 6) -> str:
    if not value:
        return "(blank)"
    if len(value) <= keep * 2:
        return value
    return f"{value[:keep]}...{value[-keep:]}"


_validate_private_key = _to_systemexit(_polymarket_primitives.validate_private_key)
_validate_address = _to_systemexit(_polymarket_primitives.validate_address)
_relayer_url = _polymarket_primitives.normalize_relayer_url
_relayer_headers = _to_systemexit(_polymarket_primitives.relayer_headers)
_request_json = _to_systemexit(_polymarket_primitives.request_json)


def _pusd_amount_to_base_units(amount: str) -> int:
    """CLI wrapper around the canonical execution-layer converter.

    ce-code-review P2 #46: delegates to execution.polymarket_transfer
    .pusd_amount_to_base_units so there's a single rounding / minimum-unit
    contract, with the boundary converting TransferSafetyError into the
    CLI's SystemExit shape.
    """
    # Lazy import to avoid the cli ↔ execution circular import at module load.
    from hightempbot.execution.polymarket_transfer import (
        TransferSafetyError,
        pusd_amount_to_base_units,
    )
    try:
        return pusd_amount_to_base_units(amount)
    except TransferSafetyError as exc:
        raise SystemExit(f"--transfer-pusd-from-proxy {exc}") from exc


_encode_contract_call = _to_systemexit(_polymarket_primitives.encode_contract_call)
_erc20_transfer_calldata = _to_systemexit(_polymarket_primitives.erc20_transfer_calldata)
_erc20_approve_calldata = _to_systemexit(_polymarket_primitives.erc20_approve_calldata)
_erc1155_set_approval_for_all_calldata = _to_systemexit(
    _polymarket_primitives.erc1155_set_approval_for_all_calldata
)


def _load_config():
    try:
        from hightempbot.runtime_config import Config
    except ImportError as exc:
        raise SystemExit(
            "Could not import hightempbot.runtime_config - run from project root "
            "with the bot environment active."
        ) from exc
    return Config()


_derive_wallets = _to_systemexit(_polymarket_primitives.derive_wallets)


def _wallet_label(address: str, wallets: WalletTopology) -> str:
    normalized = (address or "").lower()
    matches = [
        name
        for name, value in (
            ("signer/EOA", wallets.signer),
            ("proxy", wallets.proxy),
            ("safe", wallets.safe),
            ("deposit", wallets.deposit),
        )
        if normalized and normalized == value.lower()
    ]
    return ", ".join(matches) if matches else "unknown"


def _print_wallets(
    wallets: WalletTopology,
    *,
    configured_signature_type: int,
    configured_funder: str,
    relayer_key_address: str,
) -> None:
    print("Derived wallet topology")
    print(f"  signer:  {wallets.signer}")
    print(f"  proxy:   {wallets.proxy}")
    print(f"  safe:    {wallets.safe}")
    print(f"  deposit: {wallets.deposit}")
    print()
    print(
        "Configured CLOB topology: "
        f"POLY_SIGNATURE_TYPE={configured_signature_type}, "
        f"POLY_FUNDER={configured_funder or '(blank)'}"
    )
    if configured_funder:
        print(f"  POLY_FUNDER matches: {_wallet_label(configured_funder, wallets)}")
    if relayer_key_address:
        print(
            "  RELAYER_API_KEY_ADDRESS matches: "
            f"{_wallet_label(relayer_key_address, wallets)}"
        )


def _check_relayer_key(
    *,
    relayer_url: str,
    api_key: str,
    api_key_address: str,
) -> list[dict[str, Any]]:
    headers = _relayer_headers(api_key, api_key_address)
    payload = _request_json(
        "GET",
        f"{_relayer_url(relayer_url)}{RELAYER_KEY_PATH}",
        headers=headers,
    )
    if not isinstance(payload, list):
        raise SystemExit(f"Unexpected relayer key payload: {payload!r}")
    return payload


def _check_deployed(*, relayer_url: str, address: str, wallet_type: str) -> bool:
    _validate_address(address, name="wallet address")
    payload = _request_json(
        "GET",
        f"{_relayer_url(relayer_url)}{DEPLOYED_PATH}",
        params={"address": address, "type": wallet_type},
    )
    if not isinstance(payload, dict) or "deployed" not in payload:
        raise SystemExit(f"Unexpected deployed payload: {payload!r}")
    return bool(payload["deployed"])


def _deploy_deposit_wallet(
    *,
    relayer_url: str,
    api_key: str,
    api_key_address: str,
    owner: str,
) -> dict[str, Any]:
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.builder.deposit_wallet import (
            build_deposit_wallet_create_request,
        )
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "py_builder_relayer_client is not installed - install the bot deps "
            "(`pip install -e .[dev]`) before running this CLI."
        ) from exc

    owner = _validate_address(owner, name="owner")
    headers = _relayer_headers(api_key, api_key_address)
    # We only need the chain contract config; no private key or builder creds are
    # required to build a WALLET-CREATE body.
    client = RelayClient(_relayer_url(relayer_url), CHAIN_ID)
    body = build_deposit_wallet_create_request(
        owner,
        client.contract_config,
    ).to_dict()
    return _request_json(
        "POST",
        f"{_relayer_url(relayer_url)}{SUBMIT_PATH}",
        headers=headers,
        body=body,
    )


def _submit_proxy_transfer_pusd(
    *,
    relayer_url: str,
    api_key: str,
    api_key_address: str,
    private_key: str,
    to_address: str,
    amount_base_units: int,
) -> dict[str, Any]:
    try:
        from py_builder_relayer_client.builder.proxy import (
            build_proxy_transaction_request,
        )
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.encode.proxy import encode_proxy_transaction_data
        from py_builder_relayer_client.models import (
            CallType,
            ProxyTransaction,
            ProxyTransactionArgs,
            TransactionType,
        )
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "py_builder_relayer_client is not installed - install the bot deps "
            "(`pip install -e .[dev]`) before running this CLI."
        ) from exc

    headers = _relayer_headers(api_key, api_key_address)
    client = RelayClient(_relayer_url(relayer_url), CHAIN_ID, private_key)
    owner = client.signer.address()
    relay_payload = _request_json(
        "GET",
        f"{_relayer_url(relayer_url)}{RELAY_PAYLOAD_PATH}",
        params={"address": owner, "type": TransactionType.PROXY.value},
    )
    relay = str(relay_payload.get("address", ""))
    nonce = str(relay_payload.get("nonce", ""))
    if not relay or not nonce:
        raise SystemExit(f"Unexpected proxy relay payload: {relay_payload!r}")

    transaction = ProxyTransaction(
        to=PUSD_ADDRESS,
        type_code=CallType.Call,
        data=_erc20_transfer_calldata(to_address, amount_base_units),
        value="0",
    )
    encoded_data = encode_proxy_transaction_data([transaction])
    gas_limit = client._estimate_proxy_gas(
        owner,
        client.contract_config.proxy_factory,
        encoded_data,
    )
    args = ProxyTransactionArgs(
        from_address=owner,
        nonce=nonce,
        gas_price="0",
        data=encoded_data,
        relay=relay,
        gas_limit=gas_limit,
    )
    body = build_proxy_transaction_request(
        client.signer,
        args,
        client.contract_config,
        metadata="hightempbot proxy pUSD transfer to deposit wallet",
    ).to_dict()
    return _request_json(
        "POST",
        f"{_relayer_url(relayer_url)}{SUBMIT_PATH}",
        headers=headers,
        body=body,
    )


def _deposit_wallet_approval_calls() -> list[Any]:
    try:
        from py_builder_relayer_client.models import DepositWalletCall
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "py_builder_relayer_client is not installed - install the bot deps "
            "(`pip install -e .[dev]`) before running this CLI."
        ) from exc

    return [
        DepositWalletCall(
            target=PUSD_ADDRESS,
            value="0",
            data=_erc20_approve_calldata(CTF_ADDRESS),
        ),
        DepositWalletCall(
            target=PUSD_ADDRESS,
            value="0",
            data=_erc20_approve_calldata(CTF_EXCHANGE_ADDRESS),
        ),
        DepositWalletCall(
            target=PUSD_ADDRESS,
            value="0",
            data=_erc20_approve_calldata(NEG_RISK_ADAPTER_ADDRESS),
        ),
        DepositWalletCall(
            target=PUSD_ADDRESS,
            value="0",
            data=_erc20_approve_calldata(NEG_RISK_CTF_EXCHANGE_ADDRESS),
        ),
        DepositWalletCall(
            target=PUSD_ADDRESS,
            value="0",
            data=_erc20_approve_calldata(CTF_COLLATERAL_ADAPTER_ADDRESS),
        ),
        DepositWalletCall(
            target=PUSD_ADDRESS,
            value="0",
            data=_erc20_approve_calldata(NEG_RISK_CTF_COLLATERAL_ADAPTER_ADDRESS),
        ),
        DepositWalletCall(
            target=CTF_ADDRESS,
            value="0",
            data=_erc1155_set_approval_for_all_calldata(CTF_EXCHANGE_ADDRESS),
        ),
        DepositWalletCall(
            target=CTF_ADDRESS,
            value="0",
            data=_erc1155_set_approval_for_all_calldata(
                NEG_RISK_CTF_EXCHANGE_ADDRESS
            ),
        ),
        DepositWalletCall(
            target=CTF_ADDRESS,
            value="0",
            data=_erc1155_set_approval_for_all_calldata(NEG_RISK_ADAPTER_ADDRESS),
        ),
        DepositWalletCall(
            target=CTF_ADDRESS,
            value="0",
            data=_erc1155_set_approval_for_all_calldata(
                CTF_COLLATERAL_ADAPTER_ADDRESS
            ),
        ),
        DepositWalletCall(
            target=CTF_ADDRESS,
            value="0",
            data=_erc1155_set_approval_for_all_calldata(
                NEG_RISK_CTF_COLLATERAL_ADAPTER_ADDRESS
            ),
        ),
    ]


def _submit_deposit_wallet_approvals(
    *,
    relayer_url: str,
    api_key: str,
    api_key_address: str,
    private_key: str,
    deposit_wallet: str,
    deadline_seconds: int = 600,
) -> dict[str, Any]:
    try:
        from py_builder_relayer_client.builder.deposit_wallet import (
            build_deposit_wallet_batch_request,
        )
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.models import (
            DepositWalletTransactionArgs,
            TransactionType,
        )
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "py_builder_relayer_client is not installed - install the bot deps "
            "(`pip install -e .[dev]`) before running this CLI."
        ) from exc

    headers = _relayer_headers(api_key, api_key_address)
    client = RelayClient(_relayer_url(relayer_url), CHAIN_ID, private_key)
    owner = client.signer.address()
    nonce_payload = _request_json(
        "GET",
        f"{_relayer_url(relayer_url)}{NONCE_PATH}",
        params={"address": owner, "type": TransactionType.WALLET.value},
    )
    nonce = str(nonce_payload.get("nonce", ""))
    if not nonce:
        raise SystemExit(f"Unexpected deposit wallet nonce payload: {nonce_payload!r}")

    args = DepositWalletTransactionArgs(
        from_address=owner,
        chain_id=CHAIN_ID,
        wallet_address=_validate_address(deposit_wallet, name="deposit wallet"),
        nonce=nonce,
        deadline=str(int(time.time()) + deadline_seconds),
        calls=_deposit_wallet_approval_calls(),
    )
    body = build_deposit_wallet_batch_request(
        signer=client.signer,
        args=args,
        config=client.contract_config,
    ).to_dict()
    return _request_json(
        "POST",
        f"{_relayer_url(relayer_url)}{SUBMIT_PATH}",
        headers=headers,
        body=body,
    )


def _poll_transaction(
    *,
    relayer_url: str,
    transaction_id: str,
    max_polls: int = 30,
    sleep_seconds: float = 2.0,
) -> dict[str, Any] | None:
    for _ in range(max_polls):
        payload = _request_json(
            "GET",
            f"{_relayer_url(relayer_url)}{TRANSACTION_PATH}",
            params={"id": transaction_id},
        )
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            if not isinstance(row, dict):
                continue
            state = row.get("state")
            if state == CONFIRMED_STATE or state in FAILED_STATES:
                return row
        time.sleep(sleep_seconds)
    return None


def _probe_clob_balance(
    *,
    private_key: str,
    api_trio: tuple[str, str, str],
    signature_type: int,
    funder: str,
    sync_balance: bool,
) -> float:
    from hightempbot.cli.derive_poly_creds import _probe_balance

    return _probe_balance(
        private_key,
        api_trio,
        signature_type=signature_type,
        funder=funder,
        sync_balance=sync_balance,
    )


def _print_clob_probe(
    *,
    private_key: str,
    api_trio: tuple[str, str, str],
    wallets: WalletTopology,
    configured_signature_type: int,
    configured_funder: str,
    sync_balance: bool,
) -> None:
    probes = [
        ("configured", configured_signature_type, configured_funder),
        ("expected_proxy", 1, wallets.proxy),
        ("expected_deposit", 3, wallets.deposit),
    ]
    seen: set[tuple[int, str]] = set()
    print()
    print("CLOB balance probes")
    for label, signature_type, funder in probes:
        key = (int(signature_type), funder.lower())
        if key in seen:
            continue
        seen.add(key)
        try:
            balance = _probe_clob_balance(
                private_key=private_key,
                api_trio=api_trio,
                signature_type=int(signature_type),
                funder=funder,
                sync_balance=sync_balance,
            )
            print(
                f"  {label}: signature_type={signature_type}, "
                f"funder={funder or '(blank)'} -> ${balance:.2f}"
            )
        except SystemExit as exc:
            print(
                f"  {label}: signature_type={signature_type}, "
                f"funder={funder or '(blank)'} -> ERROR: {exc}"
            )


def _print_relayer_key_summary(keys: list[dict[str, Any]], api_key: str) -> None:
    matched = [
        row for row in keys
        if isinstance(row, dict) and str(row.get("apiKey", "")) == api_key
    ]
    print()
    print(f"Relayer key check: {len(keys)} key(s) returned")
    if matched:
        row = matched[0]
        print(
            "  configured key is registered to "
            f"{row.get('address', '(unknown address)')}"
        )
    else:
        print(f"  configured key {_mask(api_key)} was not in the returned key list")


def main() -> int:
    from hightempbot.cli._exitcodes import OK

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check-relayer",
        action="store_true",
        help="Validate RELAYER_API_KEY and check deposit wallet deployment.",
    )
    parser.add_argument(
        "--probe-clob",
        action="store_true",
        help="Probe CLOB pUSD balances for configured, proxy, and deposit paths.",
    )
    parser.add_argument(
        "--sync-balance",
        action="store_true",
        help="Call CLOB update_balance_allowance before each balance probe.",
    )
    parser.add_argument(
        "--deploy-deposit-wallet",
        action="store_true",
        help=(
            "Submit a relayer WALLET-CREATE transaction for the deterministic "
            "deposit wallet if it is not already deployed."
        ),
    )
    parser.add_argument(
        "--transfer-pusd-from-proxy",
        metavar="AMOUNT",
        help=(
            "Submit a proxy-wallet pUSD transfer to the deterministic deposit "
            "wallet. Amount is decimal pUSD, for example 100 or 12.34."
        ),
    )
    parser.add_argument(
        "--approve-deposit-wallet",
        action="store_true",
        help=(
            "Submit deposit-wallet approvals for pUSD and CTF trading contracts."
        ),
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Poll a submitted deploy transaction until confirmed/failed.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable topology/deployment summary.",
    )
    args = parser.parse_args()

    cfg = _load_config()
    private_key = cfg.poly_private_key.get_secret_value()
    _validate_private_key(private_key)
    relayer_url = _relayer_url(getattr(cfg, "relayer_url", DEFAULT_RELAYER_URL))
    relayer_api_key = cfg.relayer_api_key.get_secret_value()
    relayer_api_key_address = cfg.relayer_api_key_address.strip()
    configured_funder = (cfg.poly_funder or "").strip()

    wallets = _derive_wallets(private_key, relayer_url=relayer_url)
    deposit_deployed: bool | None = None
    relayer_keys: list[dict[str, Any]] | None = None
    deploy_response: dict[str, Any] | None = None
    deploy_final: dict[str, Any] | None = None
    transfer_response: dict[str, Any] | None = None
    transfer_final: dict[str, Any] | None = None
    approval_response: dict[str, Any] | None = None
    approval_final: dict[str, Any] | None = None

    needs_relayer = any([
        args.check_relayer,
        args.deploy_deposit_wallet,
        args.transfer_pusd_from_proxy,
        args.approve_deposit_wallet,
    ])

    if needs_relayer:
        relayer_keys = _check_relayer_key(
            relayer_url=relayer_url,
            api_key=relayer_api_key,
            api_key_address=relayer_api_key_address,
        )
        deposit_deployed = _check_deployed(
            relayer_url=relayer_url,
            address=wallets.deposit,
            wallet_type="WALLET",
        )

    if args.deploy_deposit_wallet and not deposit_deployed:
        deploy_response = _deploy_deposit_wallet(
            relayer_url=relayer_url,
            api_key=relayer_api_key,
            api_key_address=relayer_api_key_address,
            owner=wallets.signer,
        )
        transaction_id = str(
            deploy_response.get("transactionID")
            or deploy_response.get("transactionId")
            or ""
        )
        if args.wait and transaction_id:
            deploy_final = _poll_transaction(
                relayer_url=relayer_url,
                transaction_id=transaction_id,
            )
            deposit_deployed = _check_deployed(
                relayer_url=relayer_url,
                address=wallets.deposit,
                wallet_type="WALLET",
            )

    if args.transfer_pusd_from_proxy:
        amount_base_units = _pusd_amount_to_base_units(args.transfer_pusd_from_proxy)
        transfer_response = _submit_proxy_transfer_pusd(
            relayer_url=relayer_url,
            api_key=relayer_api_key,
            api_key_address=relayer_api_key_address,
            private_key=private_key,
            to_address=wallets.deposit,
            amount_base_units=amount_base_units,
        )
        transaction_id = str(
            transfer_response.get("transactionID")
            or transfer_response.get("transactionId")
            or ""
        )
        if args.wait and transaction_id:
            transfer_final = _poll_transaction(
                relayer_url=relayer_url,
                transaction_id=transaction_id,
            )

    if args.approve_deposit_wallet:
        if deposit_deployed is False:
            raise SystemExit(
                "Deposit wallet is not deployed. Run --deploy-deposit-wallet "
                "--wait before --approve-deposit-wallet."
            )
        approval_response = _submit_deposit_wallet_approvals(
            relayer_url=relayer_url,
            api_key=relayer_api_key,
            api_key_address=relayer_api_key_address,
            private_key=private_key,
            deposit_wallet=wallets.deposit,
        )
        transaction_id = str(
            approval_response.get("transactionID")
            or approval_response.get("transactionId")
            or ""
        )
        if args.wait and transaction_id:
            approval_final = _poll_transaction(
                relayer_url=relayer_url,
                transaction_id=transaction_id,
            )

    if args.json:
        print(json.dumps(
            {
                "wallets": wallets.__dict__,
                "configured": {
                    "signature_type": cfg.poly_signature_type,
                    "funder": configured_funder,
                    "funder_label": _wallet_label(configured_funder, wallets),
                    "relayer_key_address_label": _wallet_label(
                        relayer_api_key_address, wallets
                    ),
                },
                "relayer": {
                    "url": relayer_url,
                    "key_count": None if relayer_keys is None else len(relayer_keys),
                    "deposit_deployed": deposit_deployed,
                    "deploy_response": deploy_response,
                    "deploy_final": deploy_final,
                    "transfer_response": transfer_response,
                    "transfer_final": transfer_final,
                    "approval_response": approval_response,
                    "approval_final": approval_final,
                },
            },
            indent=2,
            sort_keys=True,
        ))
        return OK

    _print_wallets(
        wallets,
        configured_signature_type=cfg.poly_signature_type,
        configured_funder=configured_funder,
        relayer_key_address=relayer_api_key_address,
    )

    if relayer_keys is not None:
        _print_relayer_key_summary(relayer_keys, relayer_api_key)
        print(f"  deposit wallet deployed: {deposit_deployed}")

    if deploy_response is not None:
        print()
        print("Deposit wallet deploy submitted")
        print(f"  response: {deploy_response}")
        if deploy_final is not None:
            print(f"  final: {deploy_final}")
            print(f"  deposit wallet deployed after wait: {deposit_deployed}")

    if transfer_response is not None:
        print()
        print("Proxy pUSD transfer submitted")
        print(f"  response: {transfer_response}")
        if transfer_final is not None:
            print(f"  final: {transfer_final}")

    if approval_response is not None:
        print()
        print("Deposit wallet approvals submitted")
        print(f"  response: {approval_response}")
        if approval_final is not None:
            print(f"  final: {approval_final}")

    if args.probe_clob:
        api_trio = (
            cfg.poly_api_key.get_secret_value(),
            cfg.poly_secret.get_secret_value(),
            cfg.poly_passphrase.get_secret_value(),
        )
        _print_clob_probe(
            private_key=private_key,
            api_trio=api_trio,
            wallets=wallets,
            configured_signature_type=cfg.poly_signature_type,
            configured_funder=configured_funder,
            sync_balance=args.sync_balance,
        )

    return OK


if __name__ == "__main__":
    raise SystemExit(main())
