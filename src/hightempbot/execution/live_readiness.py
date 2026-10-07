"""Live readiness checks shared by startup, CLIs, dashboard and transfers:
.env config, CLOB wallet state and on-chain pUSD must all agree."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from hightempbot.polymarket.primitives import (
    PUSD_ADDRESS,
    PUSD_DECIMALS,
    WalletTopology,
    derive_wallets,
    is_address,
    mask_address,
)
from hightempbot.db.connection import parse_utc_timestamp as _parse_report_ts, utc_now_sql

if TYPE_CHECKING:
    import sqlite3

    from hightempbot.runtime_config import Config


CRITICAL_ENV_KEYS = (
    "DRY_RUN",
    "INITIAL_BANKROLL",
    "POLY_PRIVATE_KEY",
    "POLY_API_KEY",
    "POLY_SECRET",
    "POLY_PASSPHRASE",
    "POLY_SIGNATURE_TYPE",
    "POLY_FUNDER",
    "RELAYER_URL",
    "RELAYER_API_KEY",
    "RELAYER_API_KEY_ADDRESS",
    "POLY_RETURN_WALLET",
    "POLYGON_RPC_URL",
    "LIVE_READINESS_REQUIRED",
    "LIVE_REQUIRE_DEPOSIT_WALLET",
    "LIVE_ONCHAIN_VERIFY_ENABLED",
    "LIVE_BALANCE_TOLERANCE_USD",
    "WALLET_SNAPSHOT_FRESHNESS_TTL_S",
    "WALLET_SNAPSHOT_INTERVAL_MINUTES",
    "OPERATOR_ACTION_FRESHNESS_TTL_S",
)

SECRET_ENV_KEYS = {
    "POLY_PRIVATE_KEY",
    "POLY_API_KEY",
    "POLY_SECRET",
    "POLY_PASSPHRASE",
    "RELAYER_API_KEY",
    "DASHBOARD_PASS",
    "NOTIFY_TELEGRAM_TOKEN",
}

_RE_URL = re.compile(r"https?://[^\s'\"<>),]+")
_RE_SECRET_QUERY_VALUE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:key|token|secret|pass|auth)[A-Za-z0-9_.-]*=)"
    r"[^&\s'\"),;]+"
)
_RE_ADDR = re.compile(r"0x[0-9a-fA-F]{40,}")
_RE_HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")
_RE_OPAQUE_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return "<URL_REDACTED>"

    netloc = parsed.hostname or parsed.netloc.rsplit("@", 1)[-1]
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = ""
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        path = "/<REDACTED>"
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def redact_operator_text(text: str) -> str:
    """Remove URL secrets and opaque tokens from operator-visible text."""
    if not text:
        return text
    redacted = _RE_URL.sub(_redact_url, text)
    redacted = _RE_SECRET_QUERY_VALUE.sub(r"\1<REDACTED>", redacted)
    redacted = _RE_ADDR.sub("0x<REDACTED>", redacted)
    redacted = _RE_HEX64.sub("<HEX64_REDACTED>", redacted)
    redacted = _RE_OPAQUE_TOKEN.sub("<TOKEN_REDACTED>", redacted)
    return redacted


def _exception_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {redact_operator_text(str(exc))}"


def _public_check_dict(check: "CheckResult") -> dict[str, object]:
    payload = asdict(check)
    details = payload.get("details")
    if isinstance(details, dict):
        for key, value in list(details.items()):
            if key.lower() in {"error", "exception", "warning"} and isinstance(value, str):
                details[key] = redact_operator_text(value)
    return payload


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"OK", "SKIPPED"}


@dataclass(frozen=True)
class ReadinessReport:
    status: str
    mode: str
    generated_at: str
    signature_type: int
    signer: str = ""
    funder: str = ""
    derived_deposit_wallet: str = ""
    clob_balance_usd: float | None = None
    chain_balance_usd: float | None = None
    checks: list[CheckResult] = field(default_factory=list)
    expires_at: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "mode": self.mode,
            "generatedAt": self.generated_at,
            "expiresAt": self.expires_at,
            "signatureType": self.signature_type,
            "signer": self.signer,
            "funder": self.funder,
            "derivedDepositWallet": self.derived_deposit_wallet,
            "clobBalanceUsd": self.clob_balance_usd,
            "chainBalanceUsd": self.chain_balance_usd,
            "checks": [_public_check_dict(check) for check in self.checks],
        }


@dataclass(frozen=True)
class EnvParityReport:
    status: str
    generated_at: str
    compared_keys: list[str]
    mismatches: list[dict[str, str]]
    missing_local: list[str]
    missing_server: list[str]

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    def to_public_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_address(value: str | None) -> str:
    return (value or "").strip().lower()


def env_fingerprint(value: str | None) -> str:
    if value is None:
        return "missing"
    normalized = str(value).strip()
    if normalized == "":
        return "blank"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _env_value_for_compare(key: str, value: str | None) -> str | None:
    if value is None:
        return None
    if key not in SECRET_ENV_KEYS:
        return " ".join(str(value).strip().split())
    return str(value).strip()


def _parse_env_lines(lines: Iterable[str]) -> dict[str, str]:
    """Parse dotenv lines into {KEY: VALUE}, skipping comments and unquoting values."""
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env_file(path: str | Path) -> dict[str, str]:
    """Load a dotenv-style file without expanding variables or logging values."""
    env_path = Path(path)
    if not env_path.exists():
        return {}
    return _parse_env_lines(env_path.read_text(encoding="utf-8").splitlines())


def compare_env_maps(
    local_env: Mapping[str, str],
    server_env: Mapping[str, str],
    *,
    keys: Iterable[str] = CRITICAL_ENV_KEYS,
    ignore: Iterable[str] = (),
) -> EnvParityReport:
    ignore_set = {key.upper() for key in ignore}
    compared = [key for key in keys if key.upper() not in ignore_set]
    mismatches: list[dict[str, str]] = []
    missing_local: list[str] = []
    missing_server: list[str] = []

    for key in compared:
        local_present = key in local_env
        server_present = key in server_env
        if not local_present:
            missing_local.append(key)
        if not server_present:
            missing_server.append(key)
        if not local_present or not server_present:
            continue

        local_value = _env_value_for_compare(key, local_env.get(key))
        server_value = _env_value_for_compare(key, server_env.get(key))
        if local_value != server_value:
            mismatches.append(
                {
                    "key": key,
                    "localFingerprint": env_fingerprint(local_value),
                    "serverFingerprint": env_fingerprint(server_value),
                }
            )

    status = "OK" if not mismatches and not missing_local and not missing_server else "ERROR"
    return EnvParityReport(
        status=status,
        generated_at=utc_now_sql(),
        compared_keys=compared,
        mismatches=mismatches,
        missing_local=missing_local,
        missing_server=missing_server,
    )


def read_server_env_via_ssh(
    *,
    ssh_target: str,
    ssh_key: str | Path,
    server_env_path: str = "~/hightempbot/.env",
    timeout_s: int = 30,
) -> str:
    """Return the remote .env content using a read-only SSH cat."""
    def _quote_remote_path(path: str) -> str:
        if path == "~":
            return "$HOME"
        if path.startswith("~/"):
            return "$HOME/" + shlex.quote(path[2:])
        return shlex.quote(path)

    key_path = Path(ssh_key)
    cmd = [
        "ssh",
        "-i",
        str(key_path),
        "-o",
        "StrictHostKeyChecking=no",
        ssh_target,
        f"cat -- {_quote_remote_path(server_env_path)}",
    ]
    completed = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return completed.stdout


def parse_env_text(text: str) -> dict[str, str]:
    return _parse_env_lines((text or "").splitlines())


def readiness_report_is_fresh(
    report: Mapping[str, object] | None,
    *,
    freshness_ttl_s: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Return True only when a readiness report is still action-fresh."""
    if not report:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    expires_at = _parse_report_ts(report.get("expiresAt"))
    if expires_at is not None:
        return current <= expires_at

    generated_at = _parse_report_ts(report.get("generatedAt"))
    if generated_at is None or freshness_ttl_s is None:
        return False
    return (current - generated_at).total_seconds() <= max(0, freshness_ttl_s)


def build_env_parity_report(
    *,
    local_env_path: str | Path = ".env",
    server_env_text: str | None = None,
    server_env_path: str | Path | None = None,
    keys: Iterable[str] = CRITICAL_ENV_KEYS,
    ignore: Iterable[str] = (),
) -> EnvParityReport:
    local_env = load_env_file(local_env_path)
    if server_env_text is not None:
        server_env = parse_env_text(server_env_text)
    elif server_env_path is not None:
        server_env = load_env_file(server_env_path)
    else:
        server_env = {}
    return compare_env_maps(local_env, server_env, keys=keys, ignore=ignore)


def _secret_value(secret: object) -> str:
    getter = getattr(secret, "get_secret_value", None)
    if callable(getter):
        return str(getter() or "")
    return str(secret or "")


def _add_check(
    checks: list[CheckResult],
    name: str,
    status: str,
    message: str,
    **details: object,
) -> None:
    checks.append(CheckResult(name=name, status=status, message=message, details=details))


def _derive_topology(config: "Config") -> WalletTopology | None:
    private_key = _secret_value(config.poly_private_key)
    if not private_key:
        return None
    return derive_wallets(private_key, relayer_url=getattr(config, "relayer_url", ""))


def read_erc20_balance(
    *,
    rpc_url: str,
    token_address: str,
    wallet_address: str,
    decimals: int = PUSD_DECIMALS,
    timeout_s: float = 15.0,
) -> float:
    """Read an ERC-20 balance with raw JSON-RPC, avoiding a web3 dependency."""
    if not is_address(token_address):
        raise ValueError("token_address must be a 0x-prefixed 40-byte address")
    if not is_address(wallet_address):
        raise ValueError("wallet_address must be a 0x-prefixed 40-byte address")
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError("requests is not installed") from exc

    wallet_hex = wallet_address[2:].lower().rjust(64, "0")
    data = "0x70a08231" + wallet_hex
    response = requests.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": token_address, "data": data}, "latest"],
        },
        timeout=timeout_s,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    raw = str(payload.get("result") or "0x0")
    return int(raw, 16) / (10 ** decimals)


def build_live_readiness_report(
    config: "Config",
    *,
    order_client_factory: Callable[["Config"], object] | None = None,
    topology_provider: Callable[["Config"], WalletTopology | None] | None = None,
    chain_balance_reader: Callable[..., float] | None = None,
    now: datetime | None = None,
) -> ReadinessReport:
    """Run read-only checks and return an operator-safe readiness report."""
    generated = now or datetime.now(timezone.utc)
    generated_at = generated.strftime("%Y-%m-%d %H:%M:%S")
    expires = generated + timedelta(seconds=int(getattr(config, "live_readiness_cache_ttl_s", 120)))

    dry_run = bool(getattr(config, "dry_run", True))
    signature_type = int(getattr(config, "poly_signature_type", 0) or 0)
    funder = (getattr(config, "poly_funder", "") or "").strip()
    checks: list[CheckResult] = []
    signer = ""
    derived_deposit = ""
    clob_balance: float | None = None
    chain_balance: float | None = None

    if dry_run:
        _add_check(
            checks,
            "boot_mode",
            "SKIPPED",
            "DRY_RUN=True is the boot hard fuse; live readiness is informational.",
        )
        return ReadinessReport(
            status="OK",
            mode="DRY-RUN",
            generated_at=generated_at,
            expires_at=expires.strftime("%Y-%m-%d %H:%M:%S"),
            signature_type=signature_type,
            funder=funder,
            checks=checks,
        )

    private_key = _secret_value(config.poly_private_key)
    if private_key.startswith("0x") and len(private_key) == 66:
        _add_check(checks, "private_key_shape", "OK", "POLY_PRIVATE_KEY shape is valid.")
    else:
        _add_check(
            checks,
            "private_key_shape",
            "ERROR",
            "POLY_PRIVATE_KEY must be 0x-prefixed and 64 hex chars.",
        )

    missing_creds = [
        name
        for name, value in (
            ("POLY_API_KEY", _secret_value(config.poly_api_key)),
            ("POLY_SECRET", _secret_value(config.poly_secret)),
            ("POLY_PASSPHRASE", _secret_value(config.poly_passphrase)),
        )
        if not value
    ]
    if missing_creds:
        _add_check(
            checks,
            "clob_credentials",
            "ERROR",
            "Missing CLOB credential(s).",
            missing=missing_creds,
        )
    else:
        _add_check(checks, "clob_credentials", "OK", "CLOB API credential trio is present.")

    require_deposit = bool(getattr(config, "live_require_deposit_wallet", True))
    if require_deposit and signature_type != 3:
        _add_check(
            checks,
            "signature_type",
            "ERROR",
            "Live trading requires POLY_SIGNATURE_TYPE=3 for the deposit-wallet path.",
            configured=signature_type,
        )
    elif signature_type == 3:
        _add_check(checks, "signature_type", "OK", "POLY_SIGNATURE_TYPE=3 deposit wallet path.")
    else:
        _add_check(
            checks,
            "signature_type",
            "WARNING",
            "Non-deposit wallet signature type accepted by config.",
            configured=signature_type,
        )

    if signature_type == 3 and not is_address(funder):
        _add_check(
            checks,
            "funder_address",
            "ERROR",
            "POLY_FUNDER must be the 0x deposit-wallet address.",
        )
    elif funder:
        _add_check(
            checks,
            "funder_address",
            "OK",
            "POLY_FUNDER address shape is valid.",
            funder=mask_address(funder),
        )
    else:
        _add_check(checks, "funder_address", "WARNING", "POLY_FUNDER is blank.")

    topology: WalletTopology | None = None
    try:
        provider = topology_provider or _derive_topology
        topology = provider(config)
        if topology is None:
            _add_check(checks, "wallet_topology", "ERROR", "Could not derive wallet topology.")
        else:
            signer = topology.signer
            derived_deposit = topology.deposit
            _add_check(
                checks,
                "wallet_topology",
                "OK",
                "Derived signer/proxy/safe/deposit wallet topology.",
                signer=mask_address(topology.signer),
                deposit=mask_address(topology.deposit),
            )
    except Exception as exc:
        _add_check(
            checks,
            "wallet_topology",
            "ERROR",
            "Wallet topology derivation failed.",
            error=_exception_detail(exc),
        )

    if topology is not None and require_deposit:
        if normalize_address(funder) == normalize_address(topology.deposit):
            _add_check(checks, "funder_matches_deposit", "OK", "POLY_FUNDER matches derived deposit wallet.")
        else:
            _add_check(
                checks,
                "funder_matches_deposit",
                "ERROR",
                "POLY_FUNDER does not match the derived deposit wallet.",
                configured=mask_address(funder),
                expected=mask_address(topology.deposit),
            )

        relayer_owner = (getattr(config, "relayer_api_key_address", "") or "").strip()
        if normalize_address(relayer_owner) == normalize_address(topology.signer):
            _add_check(
                checks,
                "relayer_owner",
                "OK",
                "RELAYER_API_KEY_ADDRESS matches signer/EOA.",
                signer=mask_address(topology.signer),
            )
        else:
            _add_check(
                checks,
                "relayer_owner",
                "ERROR",
                "RELAYER_API_KEY_ADDRESS must match the signer/EOA for this deposit wallet.",
                configured=mask_address(relayer_owner),
                expected=mask_address(topology.signer),
            )

    if not missing_creds and private_key and all(check.name != "signature_type" or check.status != "ERROR" for check in checks):
        try:
            if order_client_factory is None:
                from hightempbot.execution.walker import OrderClient

                order_client_factory = OrderClient
            order_client = order_client_factory(config)
            balance_method = getattr(order_client, "check_balance", None)
            clob_balance = float(balance_method()) if callable(balance_method) else None
            if clob_balance is None:
                _add_check(checks, "clob_balance", "ERROR", "CLOB balance check returned no balance.")
            else:
                _add_check(
                    checks,
                    "clob_balance",
                    "OK",
                    "CLOB pUSD balance read succeeded.",
                    balanceUsd=round(clob_balance, 6),
                )
        except Exception as exc:
            _add_check(
                checks,
                "clob_balance",
                "ERROR",
                "CLOB balance check failed.",
                error=_exception_detail(exc),
            )

    if bool(getattr(config, "live_onchain_verify_enabled", True)):
        wallet_for_chain = funder or derived_deposit
        rpc_url = (getattr(config, "polygon_rpc_url", "") or "").strip()
        if not rpc_url:
            _add_check(checks, "chain_balance", "ERROR", "POLYGON_RPC_URL is required for live on-chain balance check.")
        elif not is_address(wallet_for_chain):
            _add_check(checks, "chain_balance", "ERROR", "No valid wallet address available for pUSD balanceOf.")
        else:
            try:
                reader = chain_balance_reader or read_erc20_balance
                chain_balance = float(
                    reader(
                        rpc_url=rpc_url,
                        token_address=PUSD_ADDRESS,
                        wallet_address=wallet_for_chain,
                        decimals=PUSD_DECIMALS,
                    )
                )
                _add_check(
                    checks,
                    "chain_balance",
                    "OK",
                    "On-chain pUSD balance read succeeded.",
                    balanceUsd=round(chain_balance, 6),
                )
            except Exception as exc:
                _add_check(
                    checks,
                    "chain_balance",
                    "ERROR",
                    "On-chain pUSD balance check failed.",
                    error=_exception_detail(exc),
                )
    else:
        _add_check(checks, "chain_balance", "SKIPPED", "On-chain verification disabled by config.")

    if clob_balance is not None and chain_balance is not None:
        tolerance = float(getattr(config, "live_balance_tolerance_usd", 0.25) or 0.25)
        diff = abs(clob_balance - chain_balance)
        if diff <= tolerance:
            _add_check(
                checks,
                "balance_consistency",
                "OK",
                "CLOB and on-chain pUSD balances are within tolerance.",
                diffUsd=round(diff, 6),
                toleranceUsd=tolerance,
            )
        else:
            _add_check(
                checks,
                "balance_consistency",
                "ERROR",
                "CLOB and on-chain pUSD balances disagree.",
                diffUsd=round(diff, 6),
                toleranceUsd=tolerance,
            )

    status = "OK" if all(check.status in {"OK", "SKIPPED"} for check in checks) else "ERROR"
    return ReadinessReport(
        status=status,
        mode="LIVE",
        generated_at=generated_at,
        expires_at=expires.strftime("%Y-%m-%d %H:%M:%S"),
        signature_type=signature_type,
        signer=signer,
        funder=funder,
        derived_deposit_wallet=derived_deposit,
        clob_balance_usd=clob_balance,
        chain_balance_usd=chain_balance,
        checks=checks,
    )


def record_readiness_report(conn: "sqlite3.Connection", report: ReadinessReport) -> None:
    """Persist the latest report without secrets and surface health to dashboard."""
    payload = json.dumps(report.to_public_dict(), sort_keys=True)
    try:
        conn.execute(
            """
            INSERT INTO live_readiness_reports
            (created_at, status, mode, funder, signer, clob_balance_usd,
             chain_balance_usd, report_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.generated_at,
                report.status,
                report.mode,
                report.funder,
                report.signer,
                report.clob_balance_usd,
                report.chain_balance_usd,
                payload,
            ),
        )
    except Exception:
        pass
    failing = [check.name for check in report.checks if check.status == "ERROR"]
    message = (
        "live readiness OK"
        if report.ok
        else f"live readiness failed: {', '.join(failing)[:400]}"
    )
    from hightempbot.db.connection import log_pipeline_health
    log_pipeline_health(
        conn, None, "live_readiness", "OK" if report.ok else "ERROR", message,
    )


def latest_readiness_report(conn: "sqlite3.Connection") -> dict[str, object] | None:
    try:
        row = conn.execute(
            "SELECT report_json FROM live_readiness_reports ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return None
    if row is None or not row["report_json"]:
        return None
    try:
        payload = json.loads(row["report_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
