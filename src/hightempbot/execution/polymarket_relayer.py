"""Small relayer helpers used by live operator transfer flows."""

from __future__ import annotations

import time
from typing import Any

from hightempbot.polymarket.primitives import (
    CHAIN_ID,
    CTF_COLLATERAL_ADAPTER_ADDRESS,
    NEG_RISK_CTF_COLLATERAL_ADAPTER_ADDRESS,
    NONCE_PATH,
    PUSD_ADDRESS,
    SUBMIT_PATH,
    erc20_transfer_calldata,
    normalize_relayer_url,
    redeem_positions_calldata,
    relayer_headers,
    request_json,
    validate_address,
)


class RelayerSubmitError(RuntimeError):
    """Raised when a relayer transaction cannot be built or submitted."""


def redeem_positions_target(*, negative_risk: bool) -> str:
    return (
        NEG_RISK_CTF_COLLATERAL_ADAPTER_ADDRESS
        if negative_risk
        else CTF_COLLATERAL_ADAPTER_ADDRESS
    )


def redeem_positions_deposit_wallet_call(
    *,
    condition_id: str,
    index_sets: list[int],
    negative_risk: bool,
) -> dict[str, str]:
    """Return a relayer-call dict for Polymarket pUSD redemption."""
    return {
        "target": redeem_positions_target(negative_risk=negative_risk),
        "value": "0",
        "data": redeem_positions_calldata(
            condition_id=condition_id,
            index_sets=index_sets,
        ),
    }


def submit_deposit_wallet_contract_call(
    *,
    relayer_url: str,
    api_key: str,
    api_key_address: str,
    private_key: str,
    deposit_wallet: str,
    target: str,
    data: str,
    value: str = "0",
    deadline_seconds: int = 600,
) -> dict[str, Any]:
    """Submit one arbitrary deposit-wallet contract call via the relayer."""
    try:
        from py_builder_relayer_client.builder.deposit_wallet import (
            build_deposit_wallet_batch_request,
        )
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.models import (
            DepositWalletCall,
            DepositWalletTransactionArgs,
            TransactionType,
        )
    except ModuleNotFoundError as exc:
        raise RelayerSubmitError(
            "py_builder_relayer_client is not installed; cannot submit relayer call"
        ) from exc

    if not data or not str(data).startswith("0x"):
        raise RelayerSubmitError("data must be 0x-prefixed calldata")

    relayer = normalize_relayer_url(relayer_url)
    headers = relayer_headers(api_key, api_key_address)
    client = RelayClient(relayer, CHAIN_ID, private_key)
    owner = client.signer.address()
    nonce_payload = request_json(
        "GET",
        f"{relayer}{NONCE_PATH}",
        params={"address": owner, "type": TransactionType.WALLET.value},
    )
    nonce = str(nonce_payload.get("nonce", ""))
    if not nonce:
        raise RelayerSubmitError(f"Unexpected relayer nonce payload: {nonce_payload!r}")

    call = DepositWalletCall(
        target=validate_address(target, name="target contract"),
        value=str(value or "0"),
        data=str(data),
    )
    args = DepositWalletTransactionArgs(
        from_address=owner,
        chain_id=CHAIN_ID,
        wallet_address=validate_address(deposit_wallet, name="deposit wallet"),
        nonce=nonce,
        deadline=str(int(time.time()) + deadline_seconds),
        calls=[call],
    )
    body = build_deposit_wallet_batch_request(
        signer=client.signer,
        args=args,
        config=client.contract_config,
    ).to_dict()
    return request_json(
        "POST",
        f"{relayer}{SUBMIT_PATH}",
        headers=headers,
        body=body,
    )


def submit_deposit_wallet_pusd_transfer(
    *,
    relayer_url: str,
    api_key: str,
    api_key_address: str,
    private_key: str,
    deposit_wallet: str,
    to_address: str,
    amount_base_units: int,
    deadline_seconds: int = 600,
) -> dict[str, Any]:
    """Submit a deposit-wallet pUSD transfer batch through the Polymarket relayer."""
    if amount_base_units <= 0:
        raise RelayerSubmitError("amount_base_units must be positive")

    return submit_deposit_wallet_contract_call(
        relayer_url=relayer_url,
        api_key=api_key,
        api_key_address=api_key_address,
        private_key=private_key,
        deposit_wallet=deposit_wallet,
        target=PUSD_ADDRESS,
        value="0",
        data=erc20_transfer_calldata(
            validate_address(to_address, name="return wallet"),
            amount_base_units,
        ),
        deadline_seconds=deadline_seconds,
    )


def submit_deposit_wallet_redeem_positions(
    *,
    relayer_url: str,
    api_key: str,
    api_key_address: str,
    private_key: str,
    deposit_wallet: str,
    condition_id: str,
    index_sets: list[int],
    negative_risk: bool,
    deadline_seconds: int = 600,
) -> dict[str, Any]:
    """Submit a deposit-wallet redeemPositions call through the relayer."""
    call = redeem_positions_deposit_wallet_call(
        condition_id=condition_id,
        index_sets=index_sets,
        negative_risk=negative_risk,
    )
    return submit_deposit_wallet_contract_call(
        relayer_url=relayer_url,
        api_key=api_key,
        api_key_address=api_key_address,
        private_key=private_key,
        deposit_wallet=deposit_wallet,
        target=call["target"],
        value=call["value"],
        data=call["data"],
        deadline_seconds=deadline_seconds,
    )
