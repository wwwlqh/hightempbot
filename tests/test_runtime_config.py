

from hightempbot.runtime_config import Config
from hightempbot.db.connection import get_connection, init_db


class TestConfig:
    def test_defaults_load_without_env(self):
        """Config loads with defaults when no .env exists."""
        cfg = Config(
            _env_file=None,
            poly_private_key="",
            poly_api_key="",
            poly_secret="",
            poly_passphrase="",
            ecmwf_api_key="",
        )
        assert cfg.db_path == "data/hightempbot.db"
        assert cfg.data_dir == "data"
        assert cfg.log_level == "INFO"
        assert cfg.dashboard_port == 8080
        assert cfg.relayer_url == "https://relayer-v2.polymarket.com"
        assert cfg.relayer_api_key.get_secret_value() == ""
        assert cfg.relayer_api_key_address == ""
        assert cfg.polygon_rpc_url == "https://polygon-rpc.com"
        assert cfg.poly_return_wallet == ""
        assert cfg.live_readiness_required is True
        assert cfg.live_require_deposit_wallet is True
        assert cfg.wallet_snapshot_freshness_ttl_s == 900
        assert cfg.wallet_snapshot_interval_minutes == 10
        assert cfg.auto_redeem_enabled is True
        assert cfg.auto_redeem_interval_minutes == 10

    def test_env_override(self, monkeypatch):
        """Config reads overrides from environment variables."""
        monkeypatch.setenv("DB_PATH", "/tmp/test.db")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("DASHBOARD_PORT", "9999")
        cfg = Config(_env_file=None)
        assert cfg.db_path == "/tmp/test.db"
        assert cfg.log_level == "DEBUG"
        assert cfg.dashboard_port == 9999

    def test_ensure_dirs_creates_directories(self, tmp_path):
        """ensure_dirs creates data and log directories."""
        cfg = Config(
            _env_file=None,
            db_path=str(tmp_path / "sub" / "test.db"),
            data_dir=str(tmp_path / "mydata"),
        )
        cfg.ensure_dirs()
        assert (tmp_path / "sub").is_dir()
        assert (tmp_path / "mydata").is_dir()


class TestDatabase:
    def test_get_connection_wal_mode(self, tmp_path):
        """Connection uses WAL journal mode."""
        db = tmp_path / "test.db"
        conn = get_connection(db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()

    def test_init_db_creates_tables(self, tmp_path):
        """init_db creates all expected tables from schema.sql."""
        db = tmp_path / "test.db"
        conn = init_db(db)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {
            "actuals",
            "forecast_archive",
            "calibration_params",
            "calibration_params_history",
            "pred_bucket_history",
            "lut_bucket_stats",
            "ledger",
            "signals",
            "pipeline_health",
            "enrolled_stations",
            "retrain_history",
            "market_tokens",
            "operator_control_state",
            "operator_control_events",
            "live_readiness_reports",
            "wallet_reconciliation_runs",
            "wallet_reconciliation_records",
            "transfer_requests",
            "redemption_requests",
        }
        missing = expected - tables
        assert not missing, f"schema.sql is missing expected tables: {sorted(missing)}"
        conn.close()

    def test_actuals_upsert(self, tmp_path):
        """INSERT OR REPLACE on actuals table works correctly."""
        conn = init_db(tmp_path / "test.db")
        conn.execute(
            "INSERT INTO actuals (station_id, local_date, tmax_celsius, source) "
            "VALUES (?, ?, ?, ?)",
            ("KLGA", "2024-07-15", 35.0, "gsod"),
        )
        conn.commit()
        # Upsert with new value
        conn.execute(
            "INSERT OR REPLACE INTO actuals (station_id, local_date, tmax_celsius, source) "
            "VALUES (?, ?, ?, ?)",
            ("KLGA", "2024-07-15", 36.0, "wu"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT tmax_celsius, source FROM actuals WHERE station_id='KLGA' AND local_date='2024-07-15'"
        ).fetchone()
        assert row["tmax_celsius"] == 36.0
        assert row["source"] == "wu"
        conn.close()
