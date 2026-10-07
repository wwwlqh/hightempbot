from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

from hightempbot.cli import poly_wallet_diagnostics as mod


class Secret:
    def __init__(self, value: str):
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


def _cfg(**overrides):
    values = {
        "poly_private_key": Secret("0x" + "a" * 64),
        "poly_api_key": Secret("clob_key"),
        "poly_secret": Secret("clob_secret"),
        "poly_passphrase": Secret("clob_pass"),
        "poly_signature_type": 1,
        "poly_funder": "0x" + "b" * 40,
        "relayer_url": "https://relayer.test",
        "relayer_api_key": Secret("relayer_key"),
        "relayer_api_key_address": "0x" + "a" * 40,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _wallets():
    return mod.WalletTopology(
        signer="0x" + "a" * 40,
        proxy="0x" + "b" * 40,
        safe="0x" + "c" * 40,
        deposit="0x" + "d" * 40,
    )


def test_relayer_headers_require_key_address():
    with pytest.raises(SystemExit, match="RELAYER_API_KEY_ADDRESS"):
        mod._relayer_headers("key", "not-address")


def test_wallet_label_identifies_configured_funder():
    wallets = _wallets()
    assert mod._wallet_label("0x" + "b" * 40, wallets) == "proxy"
    assert mod._wallet_label("0x" + "d" * 40, wallets) == "deposit"
    assert mod._wallet_label("0x" + "e" * 40, wallets) == "unknown"


def test_pusd_amount_conversion_uses_six_decimals():
    assert mod._pusd_amount_to_base_units("100") == 100_000_000
    assert mod._pusd_amount_to_base_units("12.3456789") == 12_345_678
    with pytest.raises(SystemExit, match="greater than 0"):
        mod._pusd_amount_to_base_units("0")


def test_erc20_transfer_calldata_shape():
    calldata = mod._erc20_transfer_calldata("0x" + "d" * 40, 100_000_000)

    assert calldata.startswith("0xa9059cbb")
    assert "dddddddddddddddddddddddddddddddddddddddd" in calldata
    assert calldata.endswith("05f5e100")


def test_deposit_wallet_approvals_include_neg_risk_adapter_ctf_approval(monkeypatch):
    models = types.ModuleType("py_builder_relayer_client.models")

    class FakeDepositWalletCall:
        def __init__(self, target, value, data):
            self.target = target
            self.value = value
            self.data = data

    models.DepositWalletCall = FakeDepositWalletCall
    monkeypatch.setitem(sys.modules, "py_builder_relayer_client.models", models)
    monkeypatch.setattr(mod, "_erc20_approve_calldata", lambda spender: f"erc20:{spender}")
    monkeypatch.setattr(
        mod,
        "_erc1155_set_approval_for_all_calldata",
        lambda operator: f"erc1155:{operator}",
    )

    calls = mod._deposit_wallet_approval_calls()

    assert any(
        call.target == mod.CTF_ADDRESS
        and call.data == f"erc1155:{mod.NEG_RISK_ADAPTER_ADDRESS}"
        for call in calls
    )


def test_json_summary_includes_wallet_matches(monkeypatch, capsys):
    monkeypatch.setattr(mod, "_load_config", lambda: _cfg())
    monkeypatch.setattr(mod, "_derive_wallets", lambda *a, **kw: _wallets())
    monkeypatch.setattr("sys.argv", ["poly_wallet_diagnostics", "--json"])

    rc = mod.main()

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["configured"]["funder_label"] == "proxy"
    assert payload["configured"]["relayer_key_address_label"] == "signer/EOA"
    assert payload["wallets"]["deposit"] == "0x" + "d" * 40


def test_check_relayer_validates_key_and_deployment(monkeypatch, capsys):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith(mod.RELAYER_KEY_PATH):
            return [{"apiKey": "relayer_key", "address": "0x" + "a" * 40}]
        if url.endswith(mod.DEPLOYED_PATH):
            return {"deployed": False}
        raise AssertionError(url)

    monkeypatch.setattr(mod, "_load_config", lambda: _cfg())
    monkeypatch.setattr(mod, "_derive_wallets", lambda *a, **kw: _wallets())
    monkeypatch.setattr(mod, "_request_json", fake_request)
    monkeypatch.setattr("sys.argv", ["poly_wallet_diagnostics", "--check-relayer"])

    rc = mod.main()

    assert rc == 0
    assert [call[0] for call in calls] == ["GET", "GET"]
    assert calls[0][2]["headers"] == {
        "RELAYER_API_KEY": "relayer_key",
        "RELAYER_API_KEY_ADDRESS": "0x" + "a" * 40,
    }
    assert calls[1][2]["params"] == {
        "address": "0x" + "d" * 40,
        "type": "WALLET",
    }
    assert "deposit wallet deployed: False" in capsys.readouterr().out


def test_deploy_deposit_wallet_skips_when_already_deployed(monkeypatch):
    deployed_calls = []

    monkeypatch.setattr(mod, "_load_config", lambda: _cfg())
    monkeypatch.setattr(mod, "_derive_wallets", lambda *a, **kw: _wallets())
    monkeypatch.setattr(
        mod,
        "_check_relayer_key",
        lambda **kw: [{"apiKey": "relayer_key", "address": "0x" + "a" * 40}],
    )

    def fake_check_deployed(**kwargs):
        deployed_calls.append(kwargs)
        return True

    monkeypatch.setattr(mod, "_check_deployed", fake_check_deployed)
    monkeypatch.setattr(
        mod,
        "_deploy_deposit_wallet",
        lambda **kw: pytest.fail("deploy should not be called"),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["poly_wallet_diagnostics", "--deploy-deposit-wallet", "--json"],
    )

    rc = mod.main()

    assert rc == 0
    assert len(deployed_calls) == 1


def test_deploy_deposit_wallet_waits_when_requested(monkeypatch, capsys):
    deployed_results = iter([False, True])
    deployed_calls = []

    monkeypatch.setattr(mod, "_load_config", lambda: _cfg())
    monkeypatch.setattr(mod, "_derive_wallets", lambda *a, **kw: _wallets())
    monkeypatch.setattr(
        mod,
        "_check_relayer_key",
        lambda **kw: [{"apiKey": "relayer_key", "address": "0x" + "a" * 40}],
    )

    def fake_check_deployed(**kwargs):
        deployed_calls.append(kwargs)
        return next(deployed_results)

    monkeypatch.setattr(mod, "_check_deployed", fake_check_deployed)
    monkeypatch.setattr(
        mod,
        "_deploy_deposit_wallet",
        lambda **kw: {"transactionID": "tx-123", "state": "STATE_NEW"},
    )
    monkeypatch.setattr(
        mod,
        "_poll_transaction",
        lambda **kw: {"transactionID": "tx-123", "state": "STATE_CONFIRMED"},
    )
    monkeypatch.setattr(
        "sys.argv",
        ["poly_wallet_diagnostics", "--deploy-deposit-wallet", "--wait", "--json"],
    )

    rc = mod.main()

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["relayer"]["deploy_response"]["transactionID"] == "tx-123"
    assert payload["relayer"]["deploy_final"]["state"] == "STATE_CONFIRMED"
    assert payload["relayer"]["deposit_deployed"] is True
    assert len(deployed_calls) == 2
