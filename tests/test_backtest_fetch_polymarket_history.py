from __future__ import annotations

import sqlite3

from backtest.scripts import fetch_polymarket_history as fph


def test_load_wu_stations_uses_poly_slug_with_city_slug_fallback(monkeypatch, tmp_path):
    db_path = tmp_path / "source.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE enrolled_stations ("
        "icao TEXT, city TEXT, resolution_source TEXT, poly_slug TEXT)"
    )
    conn.execute(
        "INSERT INTO enrolled_stations VALUES (?, ?, ?, ?)",
        ("KLGA", "New York City", "wu", "nyc"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(fph, "SOURCE_DB", db_path)

    assert fph.load_wu_stations() == [("KLGA", ("nyc", "new-york-city"))]


def test_build_city_slug_index_matches_poly_slug_alias():
    stations = [("KLGA", ("nyc", "new-york-city"))]

    assert fph._build_city_slug_index(stations)["nyc"] == "KLGA"
    assert fph._build_city_slug_index(stations)["new-york-city"] == "KLGA"


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, _url, params, timeout):
        del timeout
        self.calls.append(dict(params))
        if params["archived"] == "true":
            return _Response([
                {"slug": "highest-temperature-in-austin-on-may-11-2026"},
                {"slug": "some-other-temperature-event"},
            ])
        return _Response([
            {"slug": "highest-temperature-in-nyc-on-may-11-2026"},
        ])


def test_gamma_bulk_includes_closed_not_archived_events():
    session = _Session()

    events = fph.gamma_bulk_temperature_events(session)

    slugs = {event["slug"] for event in events}
    assert slugs == {
        "highest-temperature-in-austin-on-may-11-2026",
        "highest-temperature-in-nyc-on-may-11-2026",
    }
    assert {call["archived"] for call in session.calls} == {"true", "false"}


class _PagedSession:
    def __init__(self):
        self.calls = []

    def get(self, _url, params, timeout):
        del timeout
        self.calls.append(dict(params))
        if params["offset"] == 0:
            return _Response([
                {"slug": f"highest-temperature-in-city-{i}-on-may-10-2026"}
                for i in range(100)
            ])
        if params["offset"] == 100:
            return _Response([
                {"slug": "highest-temperature-in-nyc-on-may-11-2026"},
            ])
        return _Response([])


def test_gamma_bulk_default_pages_past_gamma_api_cap():
    session = _PagedSession()

    events = fph.gamma_bulk_temperature_events(session)

    assert any(call["offset"] == 100 for call in session.calls)
    assert "highest-temperature-in-nyc-on-may-11-2026" in {
        event["slug"] for event in events
    }
