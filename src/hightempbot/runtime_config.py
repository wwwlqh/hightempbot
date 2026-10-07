from __future__ import annotations

import threading
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Polymarket CLOB credentials
    poly_private_key: SecretStr = SecretStr("")
    poly_api_key: SecretStr = SecretStr("")
    poly_secret: SecretStr = SecretStr("")
    poly_passphrase: SecretStr = SecretStr("")

    # Public key used by wunderground.com's own web bundle.
    wu_api_key: str = Field(default="e1f10a1e78da46f5b10a1e78da96f525")

    # Trading mode
    dry_run: bool = Field(default=True)
    initial_bankroll: float = Field(default=100.0)

    # Swap NO for the FLIP sleeve at import of strategy_constants (restart to change).
    flip_mode: bool = Field(default=False)

    # 0 = EOA, 1 = POLY_PROXY, 2 = POLY_GNOSIS_SAFE, 3 = POLY_1271 deposit wallet.
    # `derive_poly_creds --probe-balance` shows which one holds your balance.
    poly_signature_type: int = Field(default=0)
    poly_funder: str = Field(default="")      # funds wallet; required for POLY_1271

    # Relayer creds (deploy deposit wallets, submit batches); separate from CLOB creds.
    relayer_url: str = Field(default="https://relayer-v2.polymarket.com")
    relayer_api_key: SecretStr = SecretStr("")
    relayer_api_key_address: str = Field(default="")

    # Live readiness / wallet reconciliation
    polygon_rpc_url: str = Field(default="https://polygon-rpc.com")
    poly_return_wallet: str = Field(default="")
    live_readiness_required: bool = Field(default=True)
    live_require_deposit_wallet: bool = Field(default=True)
    live_onchain_verify_enabled: bool = Field(default=True)
    live_balance_tolerance_usd: float = Field(default=0.25)
    live_readiness_cache_ttl_s: int = Field(default=120)
    wallet_snapshot_freshness_ttl_s: int = Field(default=900)
    wallet_snapshot_interval_minutes: int = Field(default=10)
    operator_action_freshness_ttl_s: int = Field(default=300)
    auto_redeem_enabled: bool = Field(default=True)
    auto_redeem_interval_minutes: int = Field(default=10)

    # Database & data
    db_path: str = Field(default="data/hightempbot.db")
    data_dir: str = Field(default="data")

    # Logging
    log_level: str = Field(default="INFO")

    # Dashboard
    dashboard_port: int = Field(default=8080)
    dashboard_user: str = Field(default="admin")
    dashboard_pass: str = Field(default="")
    # Empty = 0.0.0.0 if dashboard_pass is set, else 127.0.0.1.
    dashboard_bind_host: str = Field(default="")
    dashboard_tls_terminated: bool = Field(default=False)

    # Telegram alerts (skipped if unset)
    notify_telegram_token: SecretStr = SecretStr("")
    notify_telegram_chat_id: str = ""

    @property
    def db_path_resolved(self) -> Path:
        return Path(self.db_path)

    @property
    def data_dir_resolved(self) -> Path:
        return Path(self.data_dir)

    def ensure_dirs(self) -> None:
        """Create data and log directories if they don't exist."""
        self.data_dir_resolved.mkdir(parents=True, exist_ok=True)
        self.db_path_resolved.parent.mkdir(parents=True, exist_ok=True)
        Path("logs").mkdir(exist_ok=True)


# Process-wide Config, so .env is read once rather than on every tick.
_cfg_singleton: "Config | None" = None
_cfg_lock = threading.Lock()


def get_config() -> "Config":
    """Return the process-wide Config, constructing it on first call."""
    global _cfg_singleton
    if _cfg_singleton is not None:
        return _cfg_singleton
    with _cfg_lock:
        if _cfg_singleton is None:
            _cfg_singleton = Config()
        return _cfg_singleton


def set_config(cfg: "Config | None") -> None:
    """Replace the singleton (None forces a re-read). For boot and tests."""
    global _cfg_singleton
    with _cfg_lock:
        _cfg_singleton = cfg
