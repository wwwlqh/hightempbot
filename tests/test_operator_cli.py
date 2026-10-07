from __future__ import annotations

import json
from types import SimpleNamespace

from hightempbot.db.connection import get_connection, init_db


def _seed_recovered_orphan(db_path) -> int:
    conn = init_db(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO ledger
            (bet_ts, station_id, market_id, token_id, target_date,
             horizon, threshold, side, p_model, p_market, edge,
             kelly_size, volume_cap, bet_size, limit_price,
             order_id, fill_price, fill_size,
             outcome, event_type, event_detail)
            VALUES ('2026-05-20 01:00:00', 'RECOVERED', 'RECOVERED',
             'RECOVERED', 'RECOVERED', 1, 0.0, 'NO', 0.0, 0.82, 0.0,
             4.10, 0.0, 4.10, 0.82, 'ord-1', 0.82, 5.0,
             'PENDING', 'bet', ?)""",
            (json.dumps({
                "recovered_orphan": True,
                "bracket_label": "RECOVERED",
            }),),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _install_config(monkeypatch, db_path) -> None:
    from hightempbot.cli import operator

    monkeypatch.setattr(
        operator,
        "Config",
        lambda: SimpleNamespace(db_path=str(db_path), dry_run=True),
    )


def test_operator_cli_lists_recovered_orphans(tmp_path, monkeypatch, capsys):
    from hightempbot.cli import operator

    db_path = tmp_path / "orphans.db"
    orphan_id = _seed_recovered_orphan(db_path)
    _install_config(monkeypatch, db_path)

    rc = operator.main(["orphan-list"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["count"] == 1
    assert payload["orphans"][0]["ledgerId"] == orphan_id
    assert payload["orphans"][0]["limitPrice"] == 0.82
    assert captured.err == ""


def test_operator_cli_patches_recovered_orphan(tmp_path, monkeypatch, capsys):
    from hightempbot.cli import operator

    db_path = tmp_path / "orphans.db"
    orphan_id = _seed_recovered_orphan(db_path)
    _install_config(monkeypatch, db_path)

    rc = operator.main([
        "orphan-patch",
        "--id", str(orphan_id),
        "--station-id", "KDAL",
        "--target-date", "2026-05-20",
        "--market-id", "market-1",
        "--token-id", "token-1",
        "--threshold", "63.5",
        "--bracket-low", "63.5",
        "--side", "NO",
        "--bracket-label", ">=63.5F",
        "--reason", "linked from CLOB fill",
    ])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["orphan"]["stationId"] == "KDAL"
    assert captured.err == ""

    conn = get_connection(str(db_path))
    try:
        row = conn.execute(
            "SELECT station_id, market_id, token_id, target_date, threshold, "
            "side, event_detail FROM ledger WHERE id = ?",
            (orphan_id,),
        ).fetchone()
    finally:
        conn.close()

    detail = json.loads(row["event_detail"])
    assert row["station_id"] == "KDAL"
    assert row["market_id"] == "market-1"
    assert row["token_id"] == "token-1"
    assert row["target_date"] == "2026-05-20"
    assert row["threshold"] == 63.5
    assert row["side"] == "NO"
    assert detail["recovered_orphan"] is True
    assert detail["bracket_low"] == 63.5
    assert detail["bracket_high"] is None
    assert detail["bracket_label"] == ">=63.5F"
    assert detail["recovered_orphan_patch"]["actor"] == "cli:orphan-patch"
    assert detail["recovered_orphan_patch"]["reason"] == "linked from CLOB fill"
