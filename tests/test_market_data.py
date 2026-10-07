from __future__ import annotations

from datetime import date

import pytest
import requests

from hightempbot.scheduler import market_data


class _Response:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def clear_market_volume_cache():
    with market_data._market_volume_lock:
        market_data._market_volume_cache.clear()
    yield
    with market_data._market_volume_lock:
        market_data._market_volume_cache.clear()


@pytest.fixture
def gamma_slug(monkeypatch):
    import hightempbot.ingestion.polymarket_prices as polymarket_prices
    import hightempbot.stations as stations

    monkeypatch.setattr(stations, "poly_slug_for_station_id", lambda *args, **kwargs: "london")
    monkeypatch.setattr(
        polymarket_prices,
        "gamma_event_slug",
        lambda city_slug, target_date: f"{city_slug}-{target_date.isoformat()}",
    )


def test_refresh_market_volume_retries_transient_timeout(monkeypatch, gamma_slug):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            raise requests.exceptions.ReadTimeout("slow gamma")
        return _Response(payload=[{
            "markets": [{"conditionId": "mid-1", "volume": 123.45}],
        }])

    monkeypatch.setattr(requests, "get", fake_get)
    mdata = {4: {"market_id": "mid-1", "volume24hr": None}}

    market_data.refresh_market_volume("EGLC", date(2026, 6, 6), mdata)

    assert len(calls) == 2
    assert mdata[4]["volume24hr"] == 123.45


def test_populated_market_volume_seeds_timeout_fallback(monkeypatch, gamma_slug):
    seeded = {4: {"market_id": "mid-1", "volume24hr": 4196.11}}
    market_data.refresh_market_volume("EGLC", date(2026, 6, 6), seeded)

    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.exceptions.ReadTimeout("slow gamma")
        ),
    )
    mdata = {4: {"market_id": "mid-1", "volume24hr": None}}

    market_data.refresh_market_volume("EGLC", date(2026, 6, 6), mdata)

    assert mdata[4]["volume24hr"] == 4196.11


def test_expired_market_volume_cache_keeps_volume_unknown(monkeypatch, gamma_slug):
    market_data._cache_market_volumes({"mid-1": 4196.11}, now=100.0)
    monkeypatch.setattr(market_data.time, "time", lambda: 100.0 + market_data._MARKET_VOLUME_CACHE_TTL + 1)
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.exceptions.ReadTimeout("slow gamma")
        ),
    )
    mdata = {4: {"market_id": "mid-1", "volume24hr": None}}

    market_data.refresh_market_volume("EGLC", date(2026, 6, 6), mdata)

    assert mdata[4]["volume24hr"] is None
