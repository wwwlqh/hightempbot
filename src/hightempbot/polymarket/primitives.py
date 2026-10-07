"""Polymarket constants, wallet topology, and ERC-20 / relayer helpers.

ce-code-review #17/#69: shared between operator CLIs and live trading modules
(execution.live_readiness, execution.polymarket_relayer,
execution.polymarket_transfer, persistence.wallet_reconciliation).

Validation failures raise ValueError. Missing optional dependencies and HTTP
errors raise RuntimeError. The CLI wraps these into SystemExit at its own
boundary; runtime code lets them propagate or catches narrowly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


CHAIN_ID = 137
DEFAULT_RELAYER_URL = "https://relayer-v2.polymarket.com"
PUSD_DECIMALS = 6
MAX_UINT256 = (1 << 256) - 1

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_EXCHANGE_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CTF_COLLATERAL_ADAPTER_ADDRESS = "0xAdA100Db00Ca00073811820692005400218FcE1f"
NEG_RISK_CTF_COLLATERAL_ADAPTER_ADDRESS = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"

RELAYER_KEY_PATH = "/relayer/api/keys"
DEPLOYED_PATH = "/deployed"
NONCE_PATH = "/nonce"
RELAY_PAYLOAD_PATH = "/relay-payload"
SUBMIT_PATH = "/submit"
TRANSACTION_PATH = "/transaction"

CONFIRMED_STATE = "STATE_CONFIRMED"
FAILED_STATES = frozenset({"STATE_FAILED", "STATE_INVALID"})


@dataclass(frozen=True)
class WalletTopology:
    signer: str
    proxy: str
    safe: str
    deposit: str


def validate_private_key(key: str) -> None:
    if not key or not key.startswith("0x") or len(key) != 66:
        raise ValueError(
            "POLY_PRIVATE_KEY must be 0x-prefixed and 64 hex chars (66 chars total)."
        )


def is_address(value: str | None) -> bool:
    """Single address predicate shared by validate_address and all callers."""
    return bool(value and ADDRESS_RE.match(value.strip()))


def validate_address(address: str, *, name: str) -> str:
    address = (address or "").strip()
    if not is_address(address):
        raise ValueError(f"{name} must be a 0x-prefixed 40-byte address.")
    return address


def mask_address(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-4:]}"


def normalize_relayer_url(url: str) -> str:
    return (url or DEFAULT_RELAYER_URL).rstrip("/")


def relayer_headers(api_key: str, api_key_address: str) -> dict[str, str]:
    if not api_key:
        raise ValueError("RELAYER_API_KEY is empty.")
    validate_address(api_key_address, name="RELAYER_API_KEY_ADDRESS")
    return {
        "RELAYER_API_KEY": api_key,
        "RELAYER_API_KEY_ADDRESS": api_key_address,
    }


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> Any:
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError("requests is not installed.") from exc

    response = requests.request(
        method,
        url,
        headers=headers,
        params=params,
        json=body,
        timeout=timeout,
    )
    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text
    if response.status_code >= 400:
        details = payload if isinstance(payload, str) else json.dumps(payload)
        raise RuntimeError(
            f"{method} {url} failed with HTTP {response.status_code}: {details}"
        )
    return payload


def encode_contract_call(signature: str, arg_types: list[str], args: list[Any]) -> str:
    try:
        from eth_abi import encode
        from eth_utils import keccak, to_checksum_address
    except ModuleNotFoundError as exc:
        raise RuntimeError("eth_abi / eth_utils are required for relayer calldata.") from exc

    normalized_args: list[Any] = []
    for arg_type, arg in zip(arg_types, args):
        if arg_type == "address":
            normalized_args.append(to_checksum_address(validate_address(arg, name="address")))
        else:
            normalized_args.append(arg)
    selector = keccak(text=signature)[:4]
    return "0x" + (selector + encode(arg_types, normalized_args)).hex()


def erc20_transfer_calldata(to_address: str, amount_base_units: int) -> str:
    return encode_contract_call(
        "transfer(address,uint256)",
        ["address", "uint256"],
        [to_address, amount_base_units],
    )


def erc20_approve_calldata(spender: str) -> str:
    return encode_contract_call(
        "approve(address,uint256)",
        ["address", "uint256"],
        [spender, MAX_UINT256],
    )


def erc1155_set_approval_for_all_calldata(operator: str) -> str:
    return encode_contract_call(
        "setApprovalForAll(address,bool)",
        ["address", "bool"],
        [operator, True],
    )


def _validate_bytes32(value: str, *, name: str) -> bytes:
    text = (value or "").strip()
    if text.startswith("0x"):
        text = text[2:]
    if len(text) != 64 or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise ValueError(f"{name} must be a 0x-prefixed 32-byte hex value.")
    return bytes.fromhex(text)


def redeem_positions_calldata(
    *,
    condition_id: str,
    index_sets: list[int],
    collateral_token: str = PUSD_ADDRESS,
    parent_collection_id: str = "0x" + "0" * 64,
) -> str:
    """Encode Polymarket adapter redeemPositions calldata.

    Polymarket's pUSD-native redemption path routes through the collateral
    adapters and burns the caller's full token balance for the condition/index
    sets. There is intentionally no amount parameter.
    """
    if not index_sets or any(int(index) <= 0 for index in index_sets):
        raise ValueError("index_sets must contain positive CTF index set integers.")
    return encode_contract_call(
        "redeemPositions(address,bytes32,bytes32,uint256[])",
        ["address", "bytes32", "bytes32", "uint256[]"],
        [
            validate_address(collateral_token, name="collateral token"),
            _validate_bytes32(parent_collection_id, name="parent_collection_id"),
            _validate_bytes32(condition_id, name="condition_id"),
            [int(index) for index in index_sets],
        ],
    )


def derive_wallets(private_key: str, *, relayer_url: str) -> WalletTopology:
    validate_private_key(private_key)
    try:
        from py_builder_relayer_client.client import RelayClient
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "py_builder_relayer_client is not installed - install the bot deps "
            "(`pip install -e .[dev]`) before running this CLI."
        ) from exc

    client = RelayClient(normalize_relayer_url(relayer_url), CHAIN_ID, private_key)
    return WalletTopology(
        signer=client.signer.address(),
        proxy=client.get_expected_proxy_wallet(),
        safe=client.get_expected_safe(),
        deposit=client.get_expected_deposit_wallet(),
    )
