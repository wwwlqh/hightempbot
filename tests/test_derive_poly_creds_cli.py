"""Smoke tests for the Polymarket credential-derivation CLI."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from hightempbot.cli import derive_poly_creds as mod


class TestKeyValidation:
    def test_rejects_missing_0x_prefix(self, monkeypatch):
        # Stub out the CLOB import so we don't even try to construct a client.
        monkeypatch.setattr(mod, "_derive", mod._derive)
        with pytest.raises(SystemExit, match="0x"):
            mod._derive("0123456789abcdef" * 4)  # 64 hex, no 0x prefix

    def test_rejects_short_key(self):
        with pytest.raises(SystemExit, match="64 hex"):
            mod._derive("0xabc")

    def test_rejects_empty_key(self):
        with pytest.raises(SystemExit, match="0x"):
            mod._derive("")


class TestArgparse:
    def test_check_without_dot_env_trio_errors(self, monkeypatch, capsys):
        # --check + --key combo is invalid (no .env trio to compare against).
        # Patch _derive to a known trio so we can exercise the --check branch.
        monkeypatch.setattr(
            mod, "_derive",
            lambda key, **kw: ("api_x", "secret_x", "passphrase_x"),
        )
        monkeypatch.setattr(
            "sys.argv",
            ["derive_poly_creds", "--key", "0x" + "a" * 64, "--check"],
        )
        with pytest.raises(SystemExit):
            mod.main()

    def test_print_mode_outputs_env_paste(self, monkeypatch, capsys):
        monkeypatch.setattr(
            mod, "_derive",
            lambda key, **kw: ("api_xyz", "sec_xyz", "pass_xyz"),
        )
        monkeypatch.setattr(
            "sys.argv",
            ["derive_poly_creds", "--key", "0x" + "a" * 64],
        )
        rc = mod.main()
        assert rc == 0
        out = capsys.readouterr().out
        assert "POLY_API_KEY=api_xyz" in out
        assert "POLY_SECRET=sec_xyz" in out
        assert "POLY_PASSPHRASE=pass_xyz" in out
        assert "POLY_SIGNATURE_TYPE=0" in out
        assert "POLY_FUNDER=" in out

    def test_signature_type_3_requires_funder(self, monkeypatch):
        monkeypatch.setattr("sys.argv", [
            "derive_poly_creds", "--key", "0x" + "a" * 64,
            "--signature-type", "3",
        ])
        with pytest.raises(SystemExit, match="requires"):
            mod.main()

    def test_print_mode_accepts_deposit_wallet_topology(self, monkeypatch, capsys):
        monkeypatch.setattr(
            mod, "_derive",
            lambda key, **kw: ("api_xyz", "sec_xyz", "pass_xyz"),
        )
        monkeypatch.setattr("sys.argv", [
            "derive_poly_creds", "--key", "0x" + "a" * 64,
            "--signature-type", "3", "--funder", "0x" + "b" * 40,
        ])

        rc = mod.main()

        assert rc == 0
        out = capsys.readouterr().out
        assert "POLY_SIGNATURE_TYPE=3" in out
        assert "POLY_FUNDER=0x" + "b" * 40 in out

    def test_probe_balance_uses_selected_topology(self, monkeypatch, capsys):
        calls = {}

        def fake_derive(key, **kw):
            calls["derive"] = kw
            return ("api_xyz", "sec_xyz", "pass_xyz")

        def fake_probe(key, trio, **kw):
            calls["probe"] = (trio, kw)
            return 12.34

        monkeypatch.setattr(mod, "_derive", fake_derive)
        monkeypatch.setattr(mod, "_probe_balance", fake_probe)
        monkeypatch.setattr("sys.argv", [
            "derive_poly_creds", "--key", "0x" + "a" * 64,
            "--signature-type", "3", "--funder", "0x" + "b" * 40,
            "--probe-balance",
        ])

        rc = mod.main()

        assert rc == 0
        assert calls["derive"] == {
            "signature_type": 3,
            "funder": "0x" + "b" * 40,
        }
        assert calls["probe"] == (
            ("api_xyz", "sec_xyz", "pass_xyz"),
            {
                "signature_type": 3,
                "funder": "0x" + "b" * 40,
                "sync_balance": False,
            },
        )
        assert "$12.34" in capsys.readouterr().out

    def test_sync_balance_implies_probe_balance(self, monkeypatch, capsys):
        calls = {}

        monkeypatch.setattr(
            mod, "_derive",
            lambda key, **kw: ("api_xyz", "sec_xyz", "pass_xyz"),
        )

        def fake_probe(key, trio, **kw):
            calls["probe"] = kw
            return 12.34

        monkeypatch.setattr(mod, "_probe_balance", fake_probe)
        monkeypatch.setattr("sys.argv", [
            "derive_poly_creds", "--key", "0x" + "a" * 64,
            "--signature-type", "3", "--funder", "0x" + "b" * 40,
            "--sync-balance",
        ])

        rc = mod.main()

        assert rc == 0
        assert calls["probe"] == {
            "signature_type": 3,
            "funder": "0x" + "b" * 40,
            "sync_balance": True,
        }
        out = capsys.readouterr().out
        assert "CLOB balance allowance sync requested" in out
        assert "$12.34" in out


class TestClientConstruction:
    def test_derive_passes_signature_type_and_funder_to_clob_client(self, monkeypatch):
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def create_or_derive_api_key(self):
                return SimpleNamespace(
                    api_key="api",
                    api_secret="secret",
                    api_passphrase="pass",
                )

        client_mod = types.ModuleType("py_clob_client_v2.client")
        client_mod.ClobClient = FakeClient
        monkeypatch.setitem(sys.modules, "py_clob_client_v2.client", client_mod)

        trio = mod._derive(
            "0x" + "a" * 64,
            signature_type=3,
            funder="0x" + "b" * 40,
        )

        assert trio == ("api", "secret", "pass")
        assert captured["signature_type"] == 3
        assert captured["funder"] == "0x" + "b" * 40

    def test_probe_balance_can_sync_before_reading(self, monkeypatch):
        calls = []

        class FakeAssetType:
            COLLATERAL = "COLLATERAL"

        class FakeBalanceAllowanceParams:
            def __init__(self, asset_type):
                self.asset_type = asset_type

        class FakeApiCreds:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def update_balance_allowance(self, params):
                calls.append(("sync", params.asset_type))
                return {"ok": True}

            def get_balance_allowance(self, params):
                calls.append(("get", params.asset_type))
                return {"balance": "12340000", "allowances": {}}

        client_mod = types.ModuleType("py_clob_client_v2.client")
        client_mod.ClobClient = FakeClient
        clob_types_mod = types.ModuleType("py_clob_client_v2.clob_types")
        clob_types_mod.ApiCreds = FakeApiCreds
        clob_types_mod.AssetType = FakeAssetType
        clob_types_mod.BalanceAllowanceParams = FakeBalanceAllowanceParams
        monkeypatch.setitem(sys.modules, "py_clob_client_v2.client", client_mod)
        monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", clob_types_mod)

        balance = mod._probe_balance(
            "0x" + "a" * 64,
            ("api", "secret", "pass"),
            signature_type=3,
            funder="0x" + "b" * 40,
            sync_balance=True,
        )

        assert balance == pytest.approx(12.34)
        assert calls == [("sync", "COLLATERAL"), ("get", "COLLATERAL")]
