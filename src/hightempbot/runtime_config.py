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

    # Polymarket CLOB credentials (SecretStr prevents accidental logging)
    poly_private_key: SecretStr = SecretStr("")
    poly_api_key: SecretStr = SecretStr("")
    poly_secret: SecretStr = SecretStr("")
    poly_passphrase: SecretStr = SecretStr("")

    # Weather Underground API key. Defaults to the legacy public key used
    # by wunderground.com's own web bundle -- it has been live for years and
    # is genuinely public (different from the previously-committed PERSONAL
    # key that was rotated 2026-05-10 after appearing in git history).
    # Override via .env only if you have a paid IBM tier key. Probed 2026-05-21:
    # both this key and the alt "6532d6454b8aa370768e63d6ba5a832e" return
    # 200 OK on /v1/location/{ICAO}:9:{CC}/observations/historical.json.
    wu_api_key: str = Field(default="e1f10a1e78da46f5b10a1e78da96f525")

    # Trading mode
    dry_run: bool = Field(default=True)
    initial_bankroll: float = Field(default=100.0)

    # Flip-bet mode (operator experiment 2026-08-09). When True, boot swaps
    # the sleeve registry: the champion NO sleeve is disabled and the FLIP
    # sleeve (buy YES on exactly the brackets where the NO gate fires) is
    # enabled. Read once at import of strategy_constants — changing it
    # requires restart_bot.sh. Every real-fill backtest scores FLIP negative
    # (June+July 2026 flip of the live book: -$89 at real asks); this flag
    # exists because the operator explicitly ordered the live experiment.
    flip_mode: bool = Field(default=False)

    # Polymarket V2 wallet topology. The signer (poly_private_key) is the EOA
    # that signs CLOB orders, but funds typically live in a smart-contract
    # wallet derived from the EOA:
    #   0 = EOA              -- funds at the signer address (dev / on-chain trader)
    #   1 = POLY_PROXY       -- legacy proxy wallet
    #   2 = POLY_GNOSIS_SAFE -- Safe wallet
    #   3 = POLY_1271        -- deposit wallet flow for new API users
    # If unsure, run the operator diagnostic:
    #   python -m hightempbot.cli.derive_poly_creds --probe-balance
    # and pick the signature_type that returns your wallet UI balance.
    poly_signature_type: int = Field(default=0)
    # Wallet address holding funds. Required for POLY_1271 deposit wallets;
    # may be required for non-derived Safe wallets. Leave empty for EOA and
    # normally empty for POLY_PROXY.
    poly_funder: str = Field(default="")

    # Polymarket relayer credentials. These are separate from CLOB L2 creds:
    # CLOB creds authenticate orders/balances, while relayer creds deploy
    # deposit wallets and submit wallet batches.
    relayer_url: str = Field(default="https://relayer-v2.polymarket.com")
    relayer_api_key: SecretStr = SecretStr("")
    relayer_api_key_address: str = Field(default="")

    # Live readiness / wallet reconciliation. These are read-only guardrails:
    # dashboard actions cannot edit .env, and DRY_RUN remains the boot fuse.
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
    # explicit bind-host control. Defaults to loopback so
    # a fresh `.env` doesn't expose plaintext HTTP to the internet. Set to
    # "0.0.0.0" to bind publicly (live mode also requires DASHBOARD_PASS and
    # DASHBOARD_TLS_TERMINATED=1 so a reverse proxy protects admin controls).
    # Previous behavior (auto 0.0.0.0 when dashboard_pass is set) remains via
    # legacy fallback for backward compat on existing deploys.
    dashboard_bind_host: str = Field(default="")
    dashboard_tls_terminated: bool = Field(default=False)

    # Push notifications (optional — silently skipped if not set)
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


# --- Process-wide Config singleton (ce-code-review P1 #11) -----------------
# Pydantic BaseSettings re-reads .env on EVERY Config() construction. The
# scheduler used to instantiate Config() per-tick (betting_tick, jobs, etc.),
# which both wasted I/O and exposed the bot to torn-write bugs during
# operator .env edits. The singleton caches the first successful construction
# so all callers see the same instance. Credentials are read once at boot;
# rotating them requires restart_bot.sh (matches the documented workflow per
# memory `restart_bot_authorization`).
_cfg_singleton: "Config | None" = None
_cfg_lock = threading.Lock()


def get_config() -> "Config":
    """Return the process-wide Config singleton, lazy-constructing on first call.

    Thread-safe via double-checked locking. Tests that need to override
    config can call ``set_config(custom)`` before any caller hits get_config.
    """
    global _cfg_singleton
    if _cfg_singleton is not None:
        return _cfg_singleton
    with _cfg_lock:
        if _cfg_singleton is None:
            _cfg_singleton = Config()
        return _cfg_singleton


def set_config(cfg: "Config | None") -> None:
    """Override / reset the singleton. ``None`` forces a fresh re-read on
    the next ``get_config()`` call.

    Intended for tests and the boot path. Production code should NOT call
    this mid-process -- pass the boot cfg through, or call get_config().
    """
    global _cfg_singleton
    with _cfg_lock:
        _cfg_singleton = cfg
