from __future__ import annotations

import json

from hightempbot.cli import live_go_no_go
from hightempbot.db.connection import init_db


def test_live_go_no_go_offline_reaches_code_ready(tmp_path, monkeypatch, capsys):
    db = tmp_path / "test.db"
    init_db(db).close()
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("DRY_RUN", "True")

    rc = live_go_no_go.main(["--offline", "--json"])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["confidence"] in {"code-ready", "server-ready"}
    assert payload["go"] is False


def test_live_go_no_go_reports_missing_schema(tmp_path, monkeypatch, capsys):
    db = tmp_path / "empty.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("DRY_RUN", "True")

    rc = live_go_no_go.main(["--offline", "--json"])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    schema = next(c for c in payload["checks"] if c["name"] == "schema")
    assert schema["status"] == "ERROR"
