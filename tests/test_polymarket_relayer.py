"""Tests for polymarket_relayer.submit_deposit_wallet_pusd_transfer."""

from __future__ import annotations

import sys

import pytest

from hightempbot.execution.polymarket_relayer import (
    RelayerSubmitError,
    redeem_positions_deposit_wallet_call,
    submit_deposit_wallet_pusd_transfer,
)
from hightempbot.polymarket.primitives import NEG_RISK_CTF_COLLATERAL_ADAPTER_ADDRESS


def _kwargs(**overrides):
    base = dict(
        relayer_url="https://relayer.test",
        api_key="key",
        api_key_address="0x" + "a" * 40,
        private_key="0x" + "b" * 64,
        deposit_wallet="0x" + "c" * 40,
        to_address="0x" + "d" * 40,
        amount_base_units=10_000_000,
    )
    base.update(overrides)
    return base


def test_submit_raises_when_relayer_client_missing(monkeypatch):
    """py_builder_relayer_client missing must surface as RelayerSubmitError, not ImportError."""
    # Mask py_builder_relayer_client so the import inside the function fails
    # exactly like an uninstalled environment.
    blocked = {
        "py_builder_relayer_client",
        "py_builder_relayer_client.builder",
        "py_builder_relayer_client.builder.deposit_wallet",
        "py_builder_relayer_client.client",
        "py_builder_relayer_client.models",
    }
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _import(name, *args, **kwargs):
        if name in blocked:
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _import)
    # Also remove anything already imported under that name.
    for mod in list(sys.modules):
        if mod.startswith("py_builder_relayer_client"):
            sys.modules.pop(mod, None)

    with pytest.raises(RelayerSubmitError, match="not installed"):
        submit_deposit_wallet_pusd_transfer(**_kwargs())


def test_redeem_positions_call_routes_no_to_neg_risk_adapter():
    condition_id = "0x" + "1" * 64
    call = redeem_positions_deposit_wallet_call(
        condition_id=condition_id,
        index_sets=[2],
        negative_risk=True,
    )

    assert call["target"] == NEG_RISK_CTF_COLLATERAL_ADAPTER_ADDRESS
    assert call["value"] == "0"
    assert call["data"].startswith("0x")
    assert condition_id[2:] in call["data"]
    # The final ABI word is the single uint256[] element; NO is index set 2.
    assert call["data"].endswith("2".rjust(64, "0"))
