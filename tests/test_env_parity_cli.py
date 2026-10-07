from __future__ import annotations

import json

from hightempbot.cli import env_parity


def test_env_parity_cli_passes_matching_files(tmp_path, capsys):
    local = tmp_path / "local.env"
    server = tmp_path / "server.env"
    content = "DRY_RUN=False\nPOLY_FUNDER=0x" + "d" * 40 + "\n"
    local.write_text(content, encoding="utf-8")
    server.write_text(content, encoding="utf-8")

    rc = env_parity.main([
        "--local-env", str(local),
        "--server-env-file", str(server),
        "--key", "DRY_RUN",
        "--key", "POLY_FUNDER",
        "--json",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "OK"


def test_env_parity_cli_fails_mismatch_without_printing_secret(tmp_path, capsys):
    local = tmp_path / "local.env"
    server = tmp_path / "server.env"
    local.write_text("POLY_SECRET=local-secret\n", encoding="utf-8")
    server.write_text("POLY_SECRET=server-secret\n", encoding="utf-8")

    rc = env_parity.main([
        "--local-env", str(local),
        "--server-env-file", str(server),
        "--key", "POLY_SECRET",
        "--json",
    ])

    assert rc == 2
    output = capsys.readouterr().out
    assert "local-secret" not in output
    assert "server-secret" not in output
    payload = json.loads(output)
    assert payload["mismatches"][0]["key"] == "POLY_SECRET"
