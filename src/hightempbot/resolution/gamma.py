"""Gamma helpers: the /events fetch, bracket-bound parsing and the winner gate."""

from __future__ import annotations

import json
import logging
import re
from datetime import date

import requests

from hightempbot.execution.strategy_constants import RESOLUTION_PRICE_THRESHOLD
from hightempbot.ingestion.polymarket_prices import GAMMA_API, gamma_event_slug
from hightempbot.stations import poly_slug_for_station_id

logger = logging.getLogger(__name__)


def normalize_bracket_question(question: str) -> str:
    """Normalize market-question unit tokens to the format the parser expects."""
    normalized = question or ""
    for old in ("°F", "ºF", "˚F"):
        normalized = normalized.replace(old, "Â°F")
    for old in ("°C", "ºC", "˚C"):
        normalized = normalized.replace(old, "Â°C")
    return normalized


def parse_bracket_bounds(
    question: str,
) -> tuple[float | None, float | None, str] | None:
    """Continuous ``(lo, hi)`` from a market question: label X covers
    ``[X-0.5, X+0.5)``. None if unrecognized. Tolerates the ``Â°`` mojibake."""
    question = normalize_bracket_question(question)
    unit_token = r"\s*[^0-9A-Za-z-]*([FC])"

    m = re.search(rf"be (-?\d+){unit_token} or below", question)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        return None, val + 0.5, f"<{int(val)}Â°{unit}"

    m = re.search(rf"be (-?\d+){unit_token} or higher", question)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        return val - 0.5, None, f"â‰¥{int(val)}Â°{unit}"

    m = re.search(rf"between (-?\d+)-(-?\d+){unit_token}", question)
    if m:
        lo = float(m.group(1))
        hi = float(m.group(2))
        unit = m.group(3)
        if lo > hi:
            lo, hi = hi, lo
        return lo - 0.5, hi + 0.5, f"{int(lo)}-{int(hi)}Â°{unit}"

    m = re.search(rf"be (-?\d+){unit_token} on", question)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        return val - 0.5, val + 0.5, f"{int(val)}Â°{unit}"

    return None


def fetch_gamma_resolution_markets(
    station_id: str,
    target_date: date,
    *,
    conn=None,
) -> dict[int, dict] | None:
    """All of an event's markets from Gamma, closed ones included. None on failure."""
    city_slug = poly_slug_for_station_id(station_id, conn=conn)
    if not city_slug:
        return None
    slug = gamma_event_slug(city_slug, target_date)

    try:
        resp = requests.get(
            f"{GAMMA_API}/events", params={"slug": slug}, timeout=8,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            return None
        event = data[0] if isinstance(data, list) else data
        if "highest temperature" not in event.get("title", "").lower():
            return None

        out: dict[int, dict] = {}
        for bi, market in enumerate(event.get("markets", [])):
            try:
                token_ids = market.get("clobTokenIds", [])
                prices = market.get("outcomePrices", [])
                if isinstance(token_ids, str):
                    token_ids = json.loads(token_ids)
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if not token_ids or not prices:
                    continue
                yes_price = float(prices[0])
                no_price = (
                    float(prices[1]) if len(prices) > 1 else max(0.0, 1.0 - yes_price)
                )
            except Exception:
                logger.warning(
                    "Gamma resolution skipped malformed market %s for %s %s",
                    bi,
                    station_id,
                    target_date,
                    exc_info=True,
                )
                continue
            mkt_data: dict = {
                "yes_price": yes_price,
                "no_price": no_price,
                "closed": bool(market.get("closed")),
                "token_id": token_ids[0],
                "no_token_id": token_ids[1] if len(token_ids) > 1 else "",
            }
            bounds = parse_bracket_bounds(market.get("question", ""))
            if bounds:
                mkt_data["bracket_low"] = bounds[0]
                mkt_data["bracket_high"] = bounds[1]
                mkt_data["bracket_label"] = bounds[2]
            out[bi] = mkt_data
        return out or None
    except Exception:
        logger.warning(
            "Gamma resolution lookup failed for %s %s",
            station_id, target_date, exc_info=True,
        )
        return None


def winning_bracket_from_gamma(
    station_id: str,
    target_date: date,
    *,
    conn=None,
) -> dict | None:
    """The winning market, only if every bracket is closed, exactly one has
    YES ≥ RESOLUTION_PRICE_THRESHOLD, and it has bounds. Otherwise None."""
    markets = fetch_gamma_resolution_markets(station_id, target_date, conn=conn)
    if not markets:
        return None
    if not all(mkt.get("closed") for mkt in markets.values()):
        return None
    winners = [
        m for m in markets.values()
        if m.get("yes_price", 0.0) >= RESOLUTION_PRICE_THRESHOLD
    ]
    if len(winners) != 1:
        return None
    winning = winners[0]
    if winning.get("bracket_low") is None and winning.get("bracket_high") is None:
        return None
    return winning
