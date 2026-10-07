from __future__ import annotations

import json
import sqlite3

from hightempbot.cli.poly_wallet_diagnostics import WalletTopology
from hightempbot.execution.live_readiness import (
    build_live_readiness_report,
    compare_env_maps,
    env_fingerprint,
    record_readiness_report,
)
from hightempbot.runtime_config import Config


class _Client:
    def __init__(self, balance):
        self.balance = balance

    def check_balance(self):
        return self.balance


def _live_cfg(**overrides):
    values = dict(
        _env_file=None,
        dry_run=False,
        poly_private_key="0x" + "a" * 64,
        poly_api_key="key",
        poly_secret="secret",
        poly_passphrase="pass",
        poly_signature_type=3,
        poly_funder="0x" + "d" * 40,
        relayer_api_key_address="0x" + "a" * 40,
        polygon_rpc_url="https://rpc.test",
    )
    values.update(overrides)
    return Config(**values)


def _topology(_cfg):
    return WalletTopology(
        signer="0x" + "a" * 40,
        proxy="0x" + "b" * 40,
        safe="0x" + "c" * 40,
        deposit="0x" + "d" * 40,
    )


def test_env_parity_reports_secret_mismatch_by_fingerprint_only():
    report = compare_env_maps(
        {"POLY_SECRET": "local-secret", "DRY_RUN": "False"},
        {"POLY_SECRET": "server-secret", "DRY_RUN": "False"},
        keys=("POLY_SECRET", "DRY_RUN"),
    )

    assert report.status == "ERROR"
    assert report.mismatches == [{
        "key": "POLY_SECRET",
        "localFingerprint": env_fingerprint("local-secret"),
        "serverFingerprint": env_fingerprint("server-secret"),
    }]
    assert "local-secret" not in str(report.to_public_dict())
    assert "server-secret" not in str(report.to_public_dict())


def test_dry_run_readiness_skips_live_checks():
    cfg = Config(_env_file=None, dry_run=True)

    report = build_live_readiness_report(cfg)

    assert report.ok
    assert report.mode == "DRY-RUN"
    assert report.checks[0].status == "SKIPPED"


def test_live_deposit_wallet_readiness_passes_with_matching_topology():
    cfg = _live_cfg()

    report = build_live_readiness_report(
        cfg,
        topology_provider=_topology,
        order_client_factory=lambda _cfg: _Client(100.0),
        chain_balance_reader=lambda **_kw: 100.01,
    )

    assert report.ok
    assert report.funder == "0x" + "d" * 40
    assert report.clob_balance_usd == 100.0
    assert report.chain_balance_usd == 100.01


def test_live_readiness_fails_when_funder_does_not_match_deposit():
    cfg = _live_cfg(poly_funder="0x" + "e" * 40)

    report = build_live_readiness_report(
        cfg,
        topology_provider=_topology,
        order_client_factory=lambda _cfg: _Client(100.0),
        chain_balance_reader=lambda **_kw: 100.0,
    )

    assert not report.ok
    failed = {check.name for check in report.checks if check.status == "ERROR"}
    assert "funder_matches_deposit" in failed


def test_live_readiness_fails_when_clob_and_chain_balance_disagree():
    cfg = _live_cfg(live_balance_tolerance_usd=0.25)

    report = build_live_readiness_report(
        cfg,
        topology_provider=_topology,
        order_client_factory=lambda _cfg: _Client(100.0),
        chain_balance_reader=lambda **_kw: 95.0,
    )

    assert not report.ok
    failed = {check.name for check in report.checks if check.status == "ERROR"}
    assert "balance_consistency" in failed


def test_live_readiness_redacts_rpc_url_secrets_before_public_and_persisted():
    import requests

    secret_path = "sk_live_" + "x" * 44
    secret_query = "querysecret" + "y" * 44
    rpc_url = (
        "https://polygon-mainnet.g.alchemy.com/v2/"
        f"{secret_path}?apiKey={secret_query}"
    )
    cfg = _live_cfg(polygon_rpc_url=rpc_url)

    def _raise_requests_error(**kwargs):
        raise requests.exceptions.ConnectionError(
            f"failed to reach {kwargs['rpc_url']} with bearer {secret_query}"
        )

    report = build_live_readiness_report(
        cfg,
        topology_provider=_topology,
        order_client_factory=lambda _cfg: _Client(100.0),
        chain_balance_reader=_raise_requests_error,
    )

    public_text = json.dumps(report.to_public_dict(), sort_keys=True)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE live_readiness_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            status TEXT,
            mode TEXT,
            funder TEXT,
            signer TEXT,
            clob_balance_usd REAL,
            chain_balance_usd REAL,
            report_json TEXT
        )
        """
    )
    record_readiness_report(conn, report)
    stored_text = conn.execute(
        "SELECT report_json FROM live_readiness_reports ORDER BY id DESC LIMIT 1"
    ).fetchone()["report_json"]

    for text in (public_text, stored_text):
        assert "ConnectionError" in text
        assert "polygon-mainnet.g.alchemy.com" in text
        assert "<REDACTED>" in text
        assert secret_path not in text
        assert secret_query not in text
        assert "apiKey=" not in text


# ce-code-review P0 #6: read_erc20_balance unit coverage.
def test_read_erc20_balance_rejects_bad_addresses():
    import pytest

    from hightempbot.execution.live_readiness import read_erc20_balance

    with pytest.raises(ValueError, match="token_address"):
        read_erc20_balance(
            rpc_url="https://rpc.test",
            token_address="not-an-address",
            wallet_address="0x" + "a" * 40,
        )
    with pytest.raises(ValueError, match="wallet_address"):
        read_erc20_balance(
            rpc_url="https://rpc.test",
            token_address="0x" + "a" * 40,
            wallet_address="not-an-address",
        )


def test_read_erc20_balance_decodes_hex_result(monkeypatch):
    """Encodes the eth_call payload (selector 0x70a08231 + padded wallet) and decodes
    the hex JSON-RPC result by the configured decimals."""
    from hightempbot.execution import live_readiness as _lr

    captured = {}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Module:
        def post(self, url, json, timeout):  # noqa: A002 - shadows builtin via kwarg name
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            # 100 pUSD (6 decimals) = 100_000_000 = 0x5F5E100
            return _Resp({"jsonrpc": "2.0", "id": 1, "result": "0x05f5e100"})

    monkeypatch.setitem(__import__("sys").modules, "requests", _Module())

    balance = _lr.read_erc20_balance(
        rpc_url="https://rpc.test",
        token_address="0x" + "a" * 40,
        wallet_address="0x" + "b" * 40,
    )

    # 100.0 pUSD at 6 decimals.
    assert balance == 100.0
    # eth_call selector is the keccak("balanceOf(address)") prefix.
    call = captured["json"]
    assert call["method"] == "eth_call"
    data = call["params"][0]["data"]
    assert data.startswith("0x70a08231")
    # Wallet hex is padded to 32 bytes (64 hex chars).
    assert data[10:] == ("b" * 40).rjust(64, "0")


def test_read_erc20_balance_surfaces_rpc_error(monkeypatch):
    import pytest

    from hightempbot.execution import live_readiness as _lr

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}

    class _Module:
        def post(self, url, json, timeout):
            return _Resp()

    monkeypatch.setitem(__import__("sys").modules, "requests", _Module())

    with pytest.raises(RuntimeError, match="boom"):
        _lr.read_erc20_balance(
            rpc_url="https://rpc.test",
            token_address="0x" + "a" * 40,
            wallet_address="0x" + "b" * 40,
        )


# --- ce-code-review P1 #29: cover untested live_readiness branches ----------


def test_live_readiness_fails_when_clob_credentials_missing():
    """Missing CLOB API trio surfaces the clob_credentials ERROR check."""
    cfg = _live_cfg(poly_api_key="", poly_secret="", poly_passphrase="")

    report = build_live_readiness_report(
        cfg,
        topology_provider=_topology,
        order_client_factory=lambda _cfg: _Client(100.0),
        chain_balance_reader=lambda **_kw: 100.0,
    )

    assert not report.ok
    failed = {check.name for check in report.checks if check.status == "ERROR"}
    assert "clob_credentials" in failed
    cred_check = next(c for c in report.checks if c.name == "clob_credentials")
    assert set(cred_check.details.get("missing") or []) == {
        "POLY_API_KEY", "POLY_SECRET", "POLY_PASSPHRASE",
    }


def test_live_readiness_fails_when_private_key_shape_invalid():
    """Bad POLY_PRIVATE_KEY shape (too short / no 0x prefix) is caught."""
    cfg = _live_cfg(poly_private_key="not-a-real-key")

    report = build_live_readiness_report(
        cfg,
        topology_provider=_topology,
        order_client_factory=lambda _cfg: _Client(100.0),
        chain_balance_reader=lambda **_kw: 100.0,
    )

    assert not report.ok
    failed = {check.name for check in report.checks if check.status == "ERROR"}
    assert "private_key_shape" in failed


def test_live_readiness_fails_when_rpc_url_missing():
    """Empty POLYGON_RPC_URL is rejected when on-chain verify is enabled."""
    cfg = _live_cfg(polygon_rpc_url="")

    report = build_live_readiness_report(
        cfg,
        topology_provider=_topology,
        order_client_factory=lambda _cfg: _Client(100.0),
        chain_balance_reader=lambda **_kw: 100.0,
    )

    assert not report.ok
    chain_check = next(c for c in report.checks if c.name == "chain_balance")
    assert chain_check.status == "ERROR"
    assert "POLYGON_RPC_URL" in chain_check.message


def test_live_readiness_fails_when_relayer_owner_mismatches_signer():
    """RELAYER_API_KEY_ADDRESS != signer/EOA trips the relayer_owner check."""
    cfg = _live_cfg(relayer_api_key_address="0x" + "f" * 40)

    report = build_live_readiness_report(
        cfg,
        topology_provider=_topology,
        order_client_factory=lambda _cfg: _Client(100.0),
        chain_balance_reader=lambda **_kw: 100.0,
    )

    assert not report.ok
    relayer = next(c for c in report.checks if c.name == "relayer_owner")
    assert relayer.status == "ERROR"


# --- ce-code-review P2 #42: compare_env_maps missing-key branches ----------


def test_compare_env_maps_reports_missing_local_only():
    report = compare_env_maps(
        {"DRY_RUN": "False"},
        {"POLY_SECRET": "server-secret", "DRY_RUN": "False"},
        keys=("POLY_SECRET", "DRY_RUN"),
    )

    assert report.status == "ERROR"
    assert report.missing_local == ["POLY_SECRET"]
    assert report.missing_server == []
    # Mismatches list must not include keys that are simply missing on one side.
    assert report.mismatches == []


def test_compare_env_maps_reports_missing_server_only():
    report = compare_env_maps(
        {"POLY_SECRET": "local-secret", "DRY_RUN": "False"},
        {"DRY_RUN": "False"},
        keys=("POLY_SECRET", "DRY_RUN"),
    )

    assert report.status == "ERROR"
    assert report.missing_local == []
    assert report.missing_server == ["POLY_SECRET"]
    assert report.mismatches == []


# --- ce-code-review P2 #43: readiness_report_is_fresh fallback path -------


def test_readiness_report_is_fresh_uses_generated_at_ttl_when_no_expires_at():
    """When expiresAt is missing, fall back to generatedAt + ttl."""
    from datetime import datetime, timezone
    from hightempbot.execution.live_readiness import readiness_report_is_fresh

    base = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    report = {"generatedAt": "2026-05-21 12:00:00"}

    # 30s after generation, with a 60s TTL: still fresh.
    assert readiness_report_is_fresh(
        report,
        freshness_ttl_s=60,
        now=datetime(2026, 5, 21, 12, 0, 30, tzinfo=timezone.utc),
    ) is True

    # 90s after generation, with a 60s TTL: stale.
    assert readiness_report_is_fresh(
        report,
        freshness_ttl_s=60,
        now=datetime(2026, 5, 21, 12, 1, 30, tzinfo=timezone.utc),
    ) is False

    # No TTL provided: cannot judge freshness without expiresAt -> False.
    assert readiness_report_is_fresh(report, freshness_ttl_s=None, now=base) is False

    # Empty report -> False.
    assert readiness_report_is_fresh(None, freshness_ttl_s=60) is False
    assert readiness_report_is_fresh({}, freshness_ttl_s=60) is False
