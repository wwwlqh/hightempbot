"""Polymarket Gamma close-state resolution helpers.

Leaf module shared by `scheduler.station_scanner` (live settlement paths) and
`execution.ledger` (label backfill). Owns the Gamma `/events` HTTP fetch, the
question-text parser, and the winning-bracket gate so the same safety
predicates apply everywhere.
"""

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
    """Parse bracket boundaries from Polymarket question text.

    Returns the TRUE continuous [lo, hi) actual-temperature range the bracket
    resolves YES over, under ROUND-rule semantics: label X <-> actual in
    [X-0.5, X+0.5). Returns ``None`` if the question doesn't match any known
    shape.

    The regex unit token (``[^0-9A-Za-z-]*``) accepts both the literal ``°``
    and the mojibake ``Â°`` form Polymarket emits intermittently, so callers
    do not need separate normalised and raw branches.
    """
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
    """Fetch Polymarket markets for resolution via Gamma `/events`.

    Bypasses any tradability filter so closed markets (no CLOB book) are
    returned -- their `outcomePrices` are the authoritative resolution.
    Returns ``None`` on lookup failure or unknown station/event.
    """
    city_slug = poly_slug_for_station_id(station_id, conn=conn)
    if not city_slug:
        return None
    slug = gamma_event_slug(city_slug, target_date)

    try:
        # 8s timeout (was 15s): the resolution scan tick runs this twice per
        # station per cycle (event-level + per-bracket paths) directly on the
        # APScheduler thread. Cap per-call cost so a slow Gamma can't blow the
        # 20-thread pool budget. Real Gamma p99 latency is well under 5s.
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
    """Return the winning bracket market when Gamma reports a final result.

    Gate (must all hold, else returns ``None``):
    - Every bracket in the event has ``closed: True`` (excludes partial UMA).
    - Exactly one bracket has ``yes_price >= RESOLUTION_PRICE_THRESHOLD``.
    - The winning bracket has parseable bounds (``bracket_low`` and/or
      ``bracket_high`` present). Without bounds we can't render a meaningful
      label, so callers should treat the row as not-yet-resolvable rather
      than persist a placeholder.
    """
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
