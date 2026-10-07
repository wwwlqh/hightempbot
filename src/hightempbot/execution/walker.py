"""CLOB order placement and dry-run logic.

Wraps py-clob-client-v2 for order book queries and FAK limit order placement.
DRY_RUN mode logs signals without placing orders.

V2 migration (April 22, 2026): V1 orders rejected post-cutover; collateral is
pUSD (not USDC.e); OrderArgs drops nonce/fee_rate_bps/taker.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import TYPE_CHECKING

from hightempbot.db.connection import utc_now_sql
from hightempbot.execution.strategy_constants import MAX_ORDER_RETRIES, STRATEGY_CONFIGS
from hightempbot.persistence.ledger import (
    _coerce_positive_float as _coerce_pos_float,
    poly_fee_per_share,
    update_pending_bet_after_execution,
)
from hightempbot.execution.types import BetSignal, OrderResult

if TYPE_CHECKING:
    from hightempbot.runtime_config import Config

logger = logging.getLogger(__name__)

# Single-thread executor for signed CLOB client calls with timeout
_clob_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="clob")
_clob_http_semaphore = threading.BoundedSemaphore(16)
_CLOB_HTTP_BASE = "https://clob.polymarket.com"

CLOSE_SIZE_REL_TOLERANCE = 1e-6


@dataclass(frozen=True)
class _OrderExecutionSnapshot:
    status: str = ""
    fill_price: float | None = None
    fill_size: float | None = None
    fill_levels: list[dict[str, float]] | None = None
    transaction_hash: str | None = None
    trades_unknown: bool = False


class ClobClientUnavailable(RuntimeError):
    """Raised when the Polymarket V2 client is unavailable in the runtime."""


def _request_clob_json(
    path: str,
    *,
    params: dict[str, str],
    timeout_s: int,
    expected_missing: str,
) -> dict | None:
    """Fetch a public CLOB JSON endpoint, treating expected 404s as a cache miss."""
    import requests as _req

    acquired = _clob_http_semaphore.acquire(timeout=timeout_s)
    token_id = params.get("token_id", "")
    if not acquired:
        logger.warning("CLOB %s concurrency limit timed out for %s", path, token_id)
        return None
    try:
        resp = _req.get(
            f"{_CLOB_HTTP_BASE}{path}",
            params=params,
            timeout=(5, timeout_s),
        )
        if resp.status_code == 404:
            logger.debug("%s for token %s", expected_missing, token_id)
            return None
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        logger.warning("Failed to fetch CLOB %s for %s", path, token_id, exc_info=True)
        return None
    finally:
        _clob_http_semaphore.release()


# Coarse error classification used by execute_or_log to short-circuit
# non-retryable failures. ce-code-review P1 #13.
_TERMINAL_ERROR_KINDS = {"auth", "insufficient_funds", "market_closed", "invalid_amounts"}
_MATCHED_ORDER_STATUSES = {"MATCHED", "FILLED"}
_TERMINAL_ORDER_STATUSES = {"CANCELED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED"}
_SIGNATURE_TYPE_LABELS = {
    0: "EOA",
    1: "POLY_PROXY",
    2: "POLY_GNOSIS_SAFE",
    3: "POLY_1271",
}


def _classify_clob_error(exc_or_msg: object) -> str:
    """Map a CLOB exception or error string to a coarse retry-policy bucket.

    Returns one of: "auth", "insufficient_funds", "market_closed",
    "invalid_amounts", "network", "unknown". Matching is substring-based on the lower-cased
    string repr; py_clob_client_v2 surfaces these as plain RuntimeError
    instances today so the string match is what we have.
    """
    text = str(exc_or_msg or "").lower()
    if not text:
        return "unknown"
    # "below the minimum" was too broad — it matched
    # tick-size and spread errors that are NOT funding problems. Require an
    # explicit balance/funds adjacency to classify as insufficient_funds.
    if (
        "insufficient" in text
        or "not enough" in text
        or "balance too low" in text
        or re.search(r"(balance|funds).*below.*minimum", text)
        or re.search(r"below.*minimum.*(balance|funds)", text)
    ):
        return "insufficient_funds"
    if any(k in text for k in (
        "invalid amounts", "max accuracy", "maximum precision",
        "too many decimal", "tick size", "order amount",
    )):
        return "invalid_amounts"
    if any(k in text for k in (
        "signature", "unauthor", "forbidden", "invalid key",
        "api key", "api_key", "creds", "passphrase", "401", "403",
        "maker address not allowed", "deposit wallet flow",
        "signer address has to be", "address of the api key",
    )):
        return "auth"
    if any(k in text for k in (
        "market closed", "market is closed", "expired", "resolved",
        "not tradable", "not tradeable",
    )):
        return "market_closed"
    if any(k in text for k in (
        "timeout", "timed out", "connection", "connreset", "read timed",
        "max retries", "remote disconnect", "502", "503", "504",
    )):
        return "network"
    return "unknown"


def _validate_wallet_config(signature_type: int | None, funder: str | None) -> tuple[int, str]:
    """Normalize Polymarket wallet topology and fail closed on impossible config."""
    sig_type = int(signature_type or 0)
    funder = (funder or "").strip()
    if sig_type not in _SIGNATURE_TYPE_LABELS:
        allowed = ", ".join(
            f"{k}={v}" for k, v in sorted(_SIGNATURE_TYPE_LABELS.items())
        )
        raise ValueError(f"POLY_SIGNATURE_TYPE must be one of {allowed}; got {sig_type}")
    if sig_type == 3 and not funder:
        raise ValueError(
            "POLY_SIGNATURE_TYPE=3 (POLY_1271 deposit wallet) requires "
            "POLY_FUNDER=<deposit wallet address>"
        )
    return sig_type, funder


def _signed_order_field(signed_order: object, field: str) -> object | None:
    if isinstance(signed_order, dict):
        order = signed_order.get("order")
        if isinstance(order, dict) and field in order:
            return order.get(field)
        return signed_order.get(field)
    return getattr(signed_order, field, None)


def _validate_signed_order_for_wallet(
    signed_order: object,
    *,
    signature_type: int,
    funder: str,
) -> str | None:
    """Return an error when a POLY_1271 order shape would be rejected by CLOB."""
    if signature_type != 3:
        return None

    expected = (funder or "").lower()
    maker = str(_signed_order_field(signed_order, "maker") or "").lower()
    signer = str(_signed_order_field(signed_order, "signer") or "").lower()
    raw_sig_type = _signed_order_field(signed_order, "signatureType")
    signature = str(_signed_order_field(signed_order, "signature") or "")

    try:
        actual_sig_type = int(raw_sig_type)
    except (TypeError, ValueError):
        return "POLY_1271 signed order missing signatureType=3"

    if actual_sig_type != 3:
        return f"POLY_1271 signed order has signatureType={actual_sig_type}, expected 3"
    if not expected:
        return "POLY_1271 signed order missing configured funder"
    if maker != expected:
        return "POLY_1271 signed order maker does not match POLY_FUNDER"
    if signer != expected:
        return "POLY_1271 signed order signer does not match POLY_FUNDER"
    # A normal 65-byte ECDSA signature is 132 chars including 0x. Deposit
    # wallet CLOB orders need the ERC-7739/POLY_1271 wrapper built by
    # py-clob-client-v2>=1.0.1, which is materially longer.
    if len(signature) <= 132:
        return "POLY_1271 signed order signature is not ERC-1271 wrapped"
    return None


def _validate_market_buy_amount_precision(signed_order: object) -> str | None:
    """Return an error when CLOB would reject a FAK BUY amount precision."""
    raw_maker = _signed_order_field(signed_order, "makerAmount")
    raw_taker = _signed_order_field(signed_order, "takerAmount")
    if raw_maker in (None, "") and raw_taker in (None, ""):
        return None
    try:
        maker_amount = int(str(raw_maker))
        taker_amount = int(str(raw_taker))
    except (TypeError, ValueError):
        return "signed market buy order has non-integer maker/taker amounts"
    # Amounts are in 1e6 base units. CLOB accepts max 2 decimals for maker pUSD
    # and max 5 decimals for taker outcome shares on market BUY orders.
    if maker_amount % 10_000 != 0:
        return "market buy maker amount exceeds 2 decimal places"
    if taker_amount % 10 != 0:
        return "market buy taker amount exceeds 5 decimal places"
    return None


def _bounded_client_call(order_client, fn, *args, **kwargs):
    """Run ``fn(*args, **kwargs)`` under the OrderClient's timeout if available.

    Production paths route through ``order_client._with_timeout`` so a hung
    CLOB cannot block the scheduler thread indefinitely. Tests pass a stub
    client that lacks the wrapper; for those we fall back to a direct
    invocation so test assertions land. Test doubles that DO expose
    ``_with_timeout`` must provide a real pass-through callable
    (``lambda fn, *args, **kwargs: fn(*args, **kwargs)``).
    """
    wrapper = getattr(order_client, "_with_timeout", None)
    if callable(wrapper):
        return wrapper(fn, *args, **kwargs)
    return fn(*args, **kwargs)


def _level_price(level) -> float:
    """Return a numeric price for either object-style or dict-style book levels."""
    return float(level.price if hasattr(level, "price") else level["price"])


def _level_size(level) -> float:
    """Return a numeric size for either object-style or dict-style book levels."""
    return float(level.size if hasattr(level, "size") else level["size"])


def _sorted_asks(book):
    """Return ask levels sorted from cheapest to most expensive.

    Polymarket's `/book` responses can arrive in descending order, so callers
    must not assume the first ask is the best executable buy price. Always
    returns a list: a dict book missing the ``asks`` key (or a side present but
    None) coerces to ``[]`` rather than tripping ``sorted(None)``.
    """
    asks = getattr(book, "asks", None)
    if asks is None and isinstance(book, dict):
        asks = book.get("asks")
    return sorted(asks or [], key=_level_price)


def _sorted_bids(book):
    """Return bid levels sorted from cheapest to richest.

    Always returns a list: a dict book missing the ``bids`` key (live /book
    responses drop an empty side) coerces to ``[]`` rather than tripping
    ``sorted(None)``.
    """
    bids = getattr(book, "bids", None)
    if bids is None and isinstance(book, dict):
        bids = book.get("bids")
    return sorted(bids or [], key=_level_price)


def _add_fill_level(
    levels: list[dict[str, float]],
    *,
    price: float,
    shares: float,
    usd: float,
) -> None:
    """Accumulate fill notional by exact price level for operator forensics."""
    if not (
        math.isfinite(price)
        and math.isfinite(shares)
        and math.isfinite(usd)
        and price > 0
        and shares > 0
        and usd > 0
    ):
        return
    for level in levels:
        if math.isclose(level["price"], price, rel_tol=0.0, abs_tol=1e-12):
            level["shares"] += shares
            level["usd"] += usd
            return
    levels.append({"price": price, "shares": shares, "usd": usd})


def _fill_levels_from_price_size(price: float | None, shares: float | None) -> list[dict[str, float]]:
    """Build a synthetic one-level ladder when CLOB only exposes VWAP fill."""
    levels: list[dict[str, float]] = []
    if price is None or shares is None:
        return levels
    _add_fill_level(
        levels,
        price=float(price),
        shares=float(shares),
        usd=float(price) * float(shares),
    )
    return levels


def _fill_levels_from_trades(trades) -> list[dict[str, float]]:
    """Build a price-level fill ladder from CLOB trade payloads."""
    trade_list = trades if isinstance(trades, list) else ([trades] if trades else [])
    levels: list[dict[str, float]] = []
    for trade in trade_list:
        if not isinstance(trade, dict):
            continue
        try:
            price = float(trade.get("price", 0) or 0)
            shares = float(trade.get("size", 0) or 0)
        except (TypeError, ValueError):
            continue
        _add_fill_level(levels, price=price, shares=shares, usd=price * shares)
    return levels


def _tx_hash_from_trades(trades) -> str | None:
    """Return the first non-empty transaction hash from CLOB trade payloads."""
    trade_list = trades if isinstance(trades, list) else ([trades] if trades else [])
    for trade in trade_list:
        if not isinstance(trade, dict):
            continue
        tx_hash = trade.get("transactionHash") or trade.get("transaction_hash")
        if tx_hash not in (None, "", "null"):
            return str(tx_hash)
    return None


def _trade_list(trades) -> list[dict]:
    if isinstance(trades, list):
        return [trade for trade in trades if isinstance(trade, dict)]
    return [trades] if isinstance(trades, dict) else []


def _trade_matches_order(trade: dict, order_id: str) -> bool:
    if not order_id:
        return False
    direct_ids = (
        trade.get("order_id"),
        trade.get("orderID"),
        trade.get("taker_order_id"),
        trade.get("maker_order_id"),
    )
    if any(str(value or "") == order_id for value in direct_ids):
        return True
    maker_orders = trade.get("maker_orders")
    if not isinstance(maker_orders, list):
        return False
    return any(
        isinstance(order, dict) and str(order.get("order_id") or order.get("orderID") or "") == order_id
        for order in maker_orders
    )


def _filter_trades_for_order(trades, order_id: str) -> list[dict]:
    return [trade for trade in _trade_list(trades) if _trade_matches_order(trade, order_id)]


def _trade_params(*, market_id: str | None = None, token_id: str | None = None):
    try:
        from py_clob_client_v2.clob_types import TradeParams
    except (ImportError, ModuleNotFoundError):
        return None
    kwargs: dict[str, str] = {}
    if market_id:
        kwargs["market"] = str(market_id)
    if token_id:
        kwargs["asset_id"] = str(token_id)
    return TradeParams(**kwargs) if kwargs else None


def _get_trades_for_order(
    order_client,
    order_id: str,
    *,
    market_id: str | None = None,
    token_id: str | None = None,
) -> list[dict] | None:
    """Fetch CLOB trades for an order across py-clob-client versions."""
    get_trades = order_client._client.get_trades
    last_type_error: TypeError | None = None

    try:
        raw_trades = _bounded_client_call(order_client, get_trades, order_id=order_id)
        return None if raw_trades is None else _trade_list(raw_trades)
    except TypeError as exc:
        last_type_error = exc

    attempts = []
    if params := _trade_params(market_id=market_id, token_id=token_id):
        attempts.append(params)
    attempts.append(None)

    saw_response = False
    saw_unknown = False
    for params in attempts:
        try:
            raw_trades = _bounded_client_call(order_client, get_trades, params)
        except TypeError as exc:
            last_type_error = exc
            continue
        if raw_trades is None:
            saw_unknown = True
            continue
        saw_response = True
        if matches := _filter_trades_for_order(raw_trades, order_id):
            return matches

    if saw_response:
        return []
    if saw_unknown:
        return None
    if last_type_error is not None:
        raise last_type_error
    return []


_HTTPX_TIMEOUT_INSTALLED = False
_HTTPX_TIMEOUT_LOCK = threading.Lock()


def _install_clob_http_timeout() -> None:
    """Replace py_clob_client_v2's module-level httpx.Client with one that
    sets explicit per-phase timeouts.

    Library default is the httpx default Timeout(5.0) which is tight for
    CLOB under load; bot's external `_with_timeout` envelope is 30s.
    Setting (connect=10, read=30, write=30, pool=5) gives socket-level
    failures plenty of room while still bounding hung calls so workers in
    `_clob_executor` are released promptly (ce-code-review P1 #7).

    Idempotent: only patches once per process.
    """
    global _HTTPX_TIMEOUT_INSTALLED
    if _HTTPX_TIMEOUT_INSTALLED:
        return
    with _HTTPX_TIMEOUT_LOCK:
        if _HTTPX_TIMEOUT_INSTALLED:
            return
        try:
            import httpx
            from py_clob_client_v2.http_helpers import helpers as _clob_helpers
            _clob_helpers._http_client = httpx.Client(
                http2=True,
                timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=5.0),
            )
            _HTTPX_TIMEOUT_INSTALLED = True
            logger.info(
                "Installed CLOB http timeouts (connect=10s read=30s write=30s pool=5s)"
            )
        except Exception:
            logger.warning(
                "Failed to install CLOB http timeouts -- httpx default applies",
                exc_info=True,
            )


def _make_clob_client(*, key=None, creds=None, signature_type: int = 0, funder: str = ""):
    """Construct a V2 CLOB client with a clear failure mode.

    ``signature_type`` and ``funder`` control which on-chain wallet the
    client signs orders for and reads balance against:

      * 0 (EOA)              -- signer is the funder; funds at the signer address.
      * 1 (POLY_PROXY)       -- legacy Polymarket UI; proxy address is
                                deterministically derived from the signer.
      * 2 (POLY_GNOSIS_SAFE) -- Safe wallet; ``funder`` may be required if the
                                Safe is not deterministically derived.
      * 3 (POLY_1271)        -- deposit wallet flow for new API users;
                                ``funder`` is required.

    Missing or zero ``signature_type`` means "EOA". An empty ``funder`` lets
    the library auto-derive (works for EOA and POLY_PROXY; required-explicit
    for some POLY_GNOSIS_SAFE accounts).
    """
    try:
        from py_clob_client_v2.client import ClobClient
    except ModuleNotFoundError as exc:
        raise ClobClientUnavailable("py_clob_client_v2 is not installed") from exc

    _install_clob_http_timeout()

    signature_type, funder = _validate_wallet_config(signature_type, funder)

    kwargs: dict[str, object] = {
        "host": "https://clob.polymarket.com",
        "chain_id": 137,
        "key": key,
        "creds": creds,
    }
    # Only pass signature_type / funder when set so the library's None-default
    # (EOA) path stays the path for operators who haven't configured a proxy.
    if signature_type is not None and int(signature_type) != 0:
        kwargs["signature_type"] = int(signature_type)
    if funder:
        kwargs["funder"] = funder
    return ClobClient(**kwargs)


def walk_book_edge_preserving(
    book,
    target_usd: float,
    *,
    prob_safe_floor: float,
    fee_theta: float,
    min_edge: float,
    min_bet_usd: float,
    max_walk_price: float | None = None,
    walk_anchor_price: float | None = None,
    return_levels: bool = False,
) -> tuple[float, float, float, float, float] | tuple[float, float, float, float, float, list[dict[str, float]]] | None:
    """Walk ask levels up to ``target_usd`` while preserving a minimum edge.

    Before taking each level, compute the hypothetical VWAP that would result
    from consuming that level fully. If the hypothetical edge at the new VWAP
    would drop below ``min_edge``, stop BEFORE that level — we accept a
    smaller fill at a tighter realized edge rather than over-paying for depth
    the signal can't support.

    When ``max_walk_price`` is set, the walker also stops before any ask level
    whose price strictly exceeds ``walk_anchor_price + max_walk_price``. The
    two break conditions are independent — whichever fires first wins. This
    replaces the legacy flat-USD per-strategy size cap so per-bet exposure
    scales naturally with bankroll while still refusing to chase the book.

    ``walk_anchor_price`` controls what "above the cap" is measured against:
      - ``None`` (default) — anchor to the cheapest finite ask in the current
        book. Right for the scanner-time walk: the cap is "5 cents above
        whatever the market shows me right now".
      - explicit value — anchor to a fixed reference price. Retry/dry-run
        callers pass ``signal.p_market`` so the cap stays sticky to the
        scanner-time top across book drift between attempts. Without this,
        attempt 1 walks from top=0.55 (cap 0.60), attempt 2 from top=0.62
        (cap 0.67), attempt 3 from top=0.58 (cap 0.63) — the cap drifts with
        the book and the operator's "5-cent leash from where I decided" intent
        is silently violated.

    Returns ``(filled_usd, filled_shares, filled_vwap, limit_price, realized_edge)``
    by default, or appends ``fill_levels`` when ``return_levels=True``. Returns
    ``None`` when:
      - the book has no asks
      - no fill that satisfies the edge floor reaches ``min_bet_usd``
      - ``max_walk_price`` is non-finite or negative
      - ``max_walk_price`` is set but no finite top-of-book anchor can be
        derived (book has only non-finite-priced levels at the top)

    All bets produced by this walker are guaranteed to satisfy
    ``realized_edge >= min_edge`` and ``filled_usd >= min_bet_usd``.
    """
    # NaN / non-finite inputs poison comparisons (NaN < x is always False), which
    # would let the walker eat an arbitrarily bad book while still "passing" the
    # edge floor. Refuse any non-finite input up front.
    if (
        not math.isfinite(prob_safe_floor)
        or not math.isfinite(target_usd)
        or not math.isfinite(fee_theta)
        or not math.isfinite(min_edge)
        or not math.isfinite(min_bet_usd)
        or target_usd <= 0
    ):
        return None
    if max_walk_price is not None and (
        not math.isfinite(max_walk_price) or max_walk_price < 0
    ):
        logger.warning(
            "walk_book_edge_preserving rejected non-finite/negative max_walk_price=%r — "
            "config drift; refusing to walk",
            max_walk_price,
        )
        return None
    if walk_anchor_price is not None and (
        not math.isfinite(walk_anchor_price) or walk_anchor_price <= 0
    ):
        return None

    asks = _sorted_asks(book)
    if not asks:
        return None

    # Anchor the price-walk cap. Caller-supplied anchor wins (sticky retry
    # behavior); otherwise scan asks for the first finite-priced level. A book
    # whose top is NaN/Inf must not silently disable the cap by falling
    # through to no-cap behavior — fail closed.
    if max_walk_price is None:
        walk_cap_price = None
    elif walk_anchor_price is not None:
        walk_cap_price = walk_anchor_price + max_walk_price
    else:
        anchor: float | None = None
        for level in asks:
            candidate = _level_price(level)
            if math.isfinite(candidate) and candidate > 0:
                anchor = candidate
                break
        if anchor is None:
            return None
        walk_cap_price = anchor + max_walk_price

    acc_usd = 0.0
    acc_shares = 0.0
    limit_price = 0.0
    fill_levels: list[dict[str, float]] = []

    for level in asks:
        price = _level_price(level)
        size = _level_size(level)
        if not math.isfinite(price) or not math.isfinite(size) or price <= 0 or size <= 0:
            continue

        # Price-walk cap: stop before consuming any level priced strictly
        # above top_ask + max_walk_price. A level priced exactly at the cap
        # is still consumable.
        if walk_cap_price is not None and price > walk_cap_price:
            break

        remaining = target_usd - acc_usd
        if remaining <= 0:
            break

        level_usd = price * size
        take_usd = min(level_usd, remaining)
        take_shares = take_usd / price

        new_usd = acc_usd + take_usd
        new_shares = acc_shares + take_shares
        if new_shares <= 0:
            continue
        new_vwap = new_usd / new_shares
        new_fee = poly_fee_per_share(new_vwap, fee_theta=fee_theta)
        new_edge = prob_safe_floor - new_vwap - new_fee

        if not math.isfinite(new_edge) or new_edge < min_edge:
            # Adding this level breaks the floor (or produced a non-finite edge).
            break

        acc_usd = new_usd
        acc_shares = new_shares
        limit_price = price
        _add_fill_level(fill_levels, price=price, shares=take_shares, usd=take_usd)

    if acc_shares <= 0 or acc_usd < min_bet_usd:
        return None

    filled_vwap = acc_usd / acc_shares
    realized_fee = poly_fee_per_share(filled_vwap, fee_theta=fee_theta)
    realized_edge = prob_safe_floor - filled_vwap - realized_fee
    if not math.isfinite(realized_edge) or realized_edge < min_edge:
        # Defensive: numerical drift should never occur, but fail closed.
        return None
    if return_levels:
        return acc_usd, acc_shares, filled_vwap, limit_price, realized_edge, fill_levels
    return acc_usd, acc_shares, filled_vwap, limit_price, realized_edge


def _max_walk_for_signal(signal: BetSignal) -> float | None:
    """Look up the order-time price leash for ``signal``.

    Three cases:
      - **Optional slip-bounded top-up** (``signal.slip_anchor_vwap`` was set
        at decision time, and the sleeve has ``max_vwap_slip_from_anchor``):
        return that slip cap. NO/TAIL currently set the config field to None,
        so their top-ups are bounded by the realized VWAP edge floor instead.
      - **execution_min_edge sleeve, not slip-bounded** (first fill, transition
        slot, or YMID/YHIGH-style with the floor): ``None`` — the VWAP edge
        floor is the fill boundary, no 5-cent leash.
      - **everything else** (legacy ``strategy=""``): the legacy
        ``max_walk_price`` leash.
    """
    cfg = STRATEGY_CONFIGS.get(getattr(signal, "strategy", "") or "")
    if cfg is None:
        return None
    if (
        getattr(signal, "slip_anchor_vwap", None) is not None
        and cfg.max_vwap_slip_from_anchor is not None
    ):
        return cfg.max_vwap_slip_from_anchor
    if cfg.execution_min_edge is not None:
        return None
    return cfg.max_walk_price


def _walk_anchor_for_signal(signal: BetSignal) -> float | None:
    """Order-time walk anchor: optional slip anchor, else sticky scanner top.

    A signal with ``slip_anchor_vwap`` and a configured slip cap uses the slot's
    first-fill VWAP as the cap anchor. Otherwise this falls back to
    ``entry_top_price`` (the scanner-time top / slot sticky price anchor).
    """
    slip_anchor = getattr(signal, "slip_anchor_vwap", None)
    if slip_anchor is not None:
        return slip_anchor
    return signal.entry_top_price


def _min_edge_for_signal(signal: BetSignal, *, fallback: float) -> float:
    """Look up the order-time walker edge floor for ``signal``.

    Used by retry/dry-run callers so order-time re-walks match scanner-time.
    ``execution_min_edge`` overrides the strategy's pre-entry gate: NO keeps
    its 9pp entry edge gate while allowing the realized VWAP to degrade to
    3pp; TAIL keeps its vote/alpha gate while using the same 3pp execution
    floor.

    Returns ``fallback`` when the strategy is unknown; YMID still uses 0.0
    because its gate is ratio-based rather than additive-edge.
    """
    cfg = STRATEGY_CONFIGS.get(getattr(signal, "strategy", "") or "")
    if cfg is None or cfg.min_edge is None:
        if cfg is not None and cfg.execution_min_edge is not None:
            return cfg.execution_min_edge
        # YMID/TAIL: walker floor is 0.0 (their gates verified the ratio/vote
        # pre-walk; the walker just needs to keep edge non-negative). Legacy
        # rows with empty strategy: keep the historical fallback.
        if cfg is not None and cfg.min_edge is None:
            return 0.0
        return fallback
    return cfg.execution_min_edge if cfg.execution_min_edge is not None else cfg.min_edge


def _walk_bids_for_sell(book, target_shares: float) -> tuple[float, float, float] | None:
    """Walk bid levels (highest price first) to fill a SELL of ``target_shares``.

    Returns ``(fillable_shares, vwap, worst_price)`` where ``worst_price`` is
    the deepest bid we'd consume. Used by ``close_position`` so the limit on
    the close order matches the level the VWAP was computed against, and a wide
    spread is reflected as a true VWAP rather than rounded to the best bid.
    """
    if not math.isfinite(target_shares) or target_shares <= 0:
        return None
    bids = _sorted_bids(book)
    if not bids:
        return None

    acc_shares = 0.0
    acc_usd = 0.0
    worst_price = 0.0
    for level in reversed(bids):
        price = _level_price(level)
        size = _level_size(level)
        if not (math.isfinite(price) and math.isfinite(size)) or price <= 0 or size <= 0:
            continue
        remaining = target_shares - acc_shares
        if remaining <= 0:
            break
        take = min(size, remaining)
        acc_shares += take
        acc_usd += take * price
        worst_price = price
    if acc_shares <= 0 or worst_price <= 0:
        return None
    vwap = acc_usd / acc_shares
    return acc_shares, vwap, worst_price


def close_size_fills_target(fill_size: float | None, target_size: float) -> bool:
    """Return whether a close fill covers the whole target position."""
    if fill_size is None:
        return False
    try:
        size = float(fill_size)
        target = float(target_size)
    except (TypeError, ValueError):
        return False
    if not (
        math.isfinite(size)
        and math.isfinite(target)
        and size > 0
        and target > 0
    ):
        return False
    tolerance = max(1e-6, abs(target) * CLOSE_SIZE_REL_TOLERANCE)
    return size + tolerance >= target


def _quote_close_sell_with_error(
    book,
    target_size: float,
) -> tuple[tuple[float, float, float] | None, str | None]:
    walked = _walk_bids_for_sell(book, target_size)
    if walked is None:
        return None, "no executable bids"
    fillable_size, vwap, worst_price = walked
    if fillable_size <= 0 or worst_price <= 0 or vwap <= 0:
        return None, "bid book empty"
    if not close_size_fills_target(fillable_size, target_size):
        return None, (
            "insufficient bid depth to close full position: "
            f"fillable={fillable_size:.8f} target={target_size:.8f}"
        )
    return (fillable_size, vwap, worst_price), None


def quote_close_sell(book, target_size: float) -> tuple[float, float, float] | None:
    """Return a full-size executable sell quote as (size, vwap, worst_price)."""
    quote, _error = _quote_close_sell_with_error(book, target_size)
    return quote


def _has_positive_fill(fill_price: float | None, fill_size: float | None) -> bool:
    """Return True only for a parsed positive finite fill pair."""
    if fill_price is None or fill_size is None:
        return False
    try:
        price = float(fill_price)
        size = float(fill_size)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(price)
        and math.isfinite(size)
        and price > 0
        and size > 0
    )


_MARKET_BUY_SIZE_UNITS_PER_SHARE = 100
_MARKET_BUY_SPEND_QUANT = Decimal("0.01")


def _market_buy_size_float(size_units: int) -> float:
    exact_size = float(Decimal(size_units) / Decimal(_MARKET_BUY_SIZE_UNITS_PER_SHARE))
    return math.nextafter(exact_size, math.inf)


def _client_round_down(value: float, digits: int) -> Decimal:
    factor = 10 ** digits
    return Decimal(str(math.floor(float(value) * factor) / factor))


def _finite_decimal(raw: object) -> Decimal | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return value


def _decimal_units_and_scale(value: Decimal) -> tuple[int, int]:
    normalized = value.normalize()
    exponent = normalized.as_tuple().exponent
    scale = 10 ** max(0, -exponent)
    units = int((normalized * scale).to_integral_value())
    return units, scale


def _quantize_market_buy_size(
    target_usd: float,
    order_price: float,
    *,
    min_bet_usd: float,
) -> tuple[float, float] | None:
    """Return (shares, max_spend_usd) that CLOB market-buy precision accepts."""
    target = _finite_decimal(target_usd)
    price = _finite_decimal(order_price)
    min_bet = _finite_decimal(min_bet_usd)
    if (
        target is None
        or price is None
        or min_bet is None
        or target <= 0
        or price <= 0
        or min_bet < 0
    ):
        return None

    size_scale = Decimal(_MARKET_BUY_SIZE_UNITS_PER_SHARE)
    max_size_units = int(((target / price) * size_scale).to_integral_value(rounding=ROUND_DOWN))
    if max_size_units <= 0:
        return None

    price_units, price_scale = _decimal_units_and_scale(price)
    if price_units <= 0 or price_scale <= 0:
        return None

    # py-clob signs BUY orders on a centi-share grid, then derives maker
    # collateral from limit_price * size. Make the centi-share count a multiple
    # that keeps the derived maker amount to whole cents before signing.
    cent_exact_step_units = price_scale // math.gcd(abs(price_units), price_scale)
    size_units = max_size_units - (max_size_units % cent_exact_step_units)
    while size_units > 0:
        # py-clob-client-v2 applies math.floor(float(size) * 100) / 100.
        # Values like 80.6 can become 80.59 when represented as a float, which
        # turns a valid $4.03 maker amount at 5c into rejected $4.0295. Nudge
        # the chosen cent-share float upward by one representable step so the
        # client's floor lands on the intended size.
        size_float = _market_buy_size_float(size_units)
        client_size = _client_round_down(size_float, 2)
        spend = price * client_size
        spend_cents = spend.quantize(_MARKET_BUY_SPEND_QUANT, rounding=ROUND_DOWN)
        if (
            spend == spend_cents
            and spend_cents >= min_bet
            and spend_cents <= target
        ):
            return size_float, float(spend_cents)
        size_units -= cent_exact_step_units
    return None


def quantize_market_buy_size(
    target_usd: float,
    order_price: float,
    *,
    min_bet_usd: float,
) -> tuple[float, float] | None:
    """Return orderable BUY size/spend on the same grid used for signing."""
    return _quantize_market_buy_size(
        target_usd,
        order_price,
        min_bet_usd=min_bet_usd,
    )


def _extract_close_fill(response) -> tuple[float | None, float | None]:
    """Best-effort parse of (avg_price, size) from a CLOB post_order response."""
    if not isinstance(response, dict):
        return None, None
    price = _coerce_pos_float(
        response.get("avg_price")
        or response.get("matched_avg_price")
        or response.get("price_matched")
        or response.get("price")
    )
    size = _coerce_pos_float(
        response.get("size_matched")
        or response.get("matched_size")
        or response.get("filled_size")
        or response.get("size")
    )
    return price, size


def _extract_matched_fill(order) -> tuple[float | None, float | None]:
    """Best-effort parse of (avg_price, size) from a CLOB ``get_order`` payload."""
    if order is None:
        return None, None
    if isinstance(order, dict):
        getter = order.get
    else:
        getter = lambda key, default=None: getattr(order, key, default)
    price = _coerce_pos_float(
        getter("avg_price")
        or getter("matched_avg_price")
        or getter("price_matched")
        or getter("avgPrice")
        or getter("price")
    )
    size = _coerce_pos_float(
        getter("size_matched")
        or getter("matched_size")
        or getter("filled_size")
        or getter("sizeMatched")
        or getter("size")
    )
    return price, size


def _read_order_execution_snapshot(order_client, order_id: str) -> _OrderExecutionSnapshot:
    """Read actual fill data for a posted order from trades and order status."""
    status = ""
    fill_price = None
    fill_size = None
    fill_levels: list[dict[str, float]] = []
    tx_hash = None
    trades_unknown = False

    try:
        raw_trades = _get_trades_for_order(order_client, order_id)
    except FuturesTimeoutError:
        logger.warning("get_trades timed out while reading execution for %s", order_id)
        raw_trades = None
        trades_unknown = True
    except Exception:
        logger.debug("get_trades failed while reading execution for %s", order_id, exc_info=True)
        raw_trades = None
        trades_unknown = True

    if raw_trades is None:
        trades_unknown = True
    else:
        tx_hash = _tx_hash_from_trades(raw_trades)
        fill_levels = _fill_levels_from_trades(raw_trades)
        if fill_levels:
            notional = sum(level["usd"] for level in fill_levels)
            shares = sum(level["shares"] for level in fill_levels)
            if shares > 0:
                fill_price = notional / shares
                fill_size = shares

    try:
        raw_order = _bounded_client_call(
            order_client, order_client._client.get_order, order_id
        )
        if raw_order is not None:
            status_raw = (
                raw_order.get("status")
                if isinstance(raw_order, dict)
                else getattr(raw_order, "status", "")
            )
            status = str(status_raw or "").upper()
            order_price, order_size = _extract_matched_fill(raw_order)
            if fill_price is None:
                fill_price = order_price
            if fill_size is None:
                fill_size = order_size
            if not fill_levels:
                fill_levels = _fill_levels_from_price_size(fill_price, fill_size)
    except FuturesTimeoutError:
        logger.warning("get_order timed out while reading execution for %s", order_id)
    except Exception:
        logger.debug("get_order failed while reading execution for %s", order_id, exc_info=True)

    return _OrderExecutionSnapshot(
        status=status,
        fill_price=fill_price,
        fill_size=fill_size,
        fill_levels=fill_levels,
        transaction_hash=tx_hash,
        trades_unknown=trades_unknown,
    )


def _realized_edge_for_fill(
    signal: BetSignal,
    fill_price: float | None,
    *,
    fallback: float | None,
    fee_theta: float,
) -> float | None:
    """Recompute realized edge when actual fill price differs from scanner price."""
    if fill_price is None:
        return fallback
    try:
        prob_floor = float(_prob_safe_floor(signal))
        price = float(fill_price)
    except (TypeError, ValueError):
        return fallback
    if not (math.isfinite(prob_floor) and math.isfinite(price)):
        return fallback
    fee = poly_fee_per_share(price, fee_theta=fee_theta)
    return prob_floor - price - fee


def _fetch_order_book(timeout_s: int, token_id: str) -> dict | None:
    return _request_clob_json(
        "/book",
        params={"token_id": token_id},
        timeout_s=timeout_s,
        expected_missing="CLOB order book missing",
    )


def _best_ask(book) -> tuple[float, float] | None:
    asks = _sorted_asks(book)
    if not asks:
        return None
    best = asks[0]
    return _level_price(best), _level_size(best)


def _best_bid(book) -> tuple[float, float] | None:
    bids = _sorted_bids(book)
    if not bids:
        return None
    best = bids[-1]
    return _level_price(best), _level_size(best)


class ClobReader:
    """Read-only CLOB client for order book queries. No credentials needed."""

    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    def fetch_order_book(self, token_id: str) -> dict | None:
        return _fetch_order_book(self._timeout, token_id)

    def best_ask(self, book) -> tuple[float, float] | None:
        return _best_ask(book)

    def best_bid(self, book) -> tuple[float, float] | None:
        return _best_bid(book)

    def fetch_price(self, token_id: str, side: str = "buy") -> float | None:
        """Fetch executable price from CLOB /price endpoint.

        The /price endpoint returns the real executable price from Polymarket's
        matching engine, unlike /book which may show market-maker wall stubs.

        Args:
            token_id: Polymarket CLOB token ID.
            side: "buy" or "sell".

        Returns:
            Price as float, or None on error/timeout.
        """
        data = _request_clob_json(
            "/price",
            params={"token_id": token_id, "side": side},
            timeout_s=self._timeout,
            expected_missing="CLOB executable price missing",
        )
        if data is None:
            return None
        try:
            price = float(data.get("price"))
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

class OrderClient:
    """Full CLOB client for order placement. Requires API credentials."""

    def __init__(self, config: Config) -> None:
        try:
            from py_clob_client_v2.clob_types import ApiCreds
        except ModuleNotFoundError as exc:
            raise ClobClientUnavailable("py_clob_client_v2 is not installed") from exc

        key = config.poly_private_key.get_secret_value()
        if not key:
            raise ValueError("poly_private_key is required for OrderClient")

        creds = ApiCreds(
            api_key=config.poly_api_key.get_secret_value(),
            api_secret=config.poly_secret.get_secret_value(),
            api_passphrase=config.poly_passphrase.get_secret_value(),
        )

        # Polymarket V2 wallet topology -- read from config so operators with
        # POLY_PROXY (legacy email/MagicLink) or POLY_GNOSIS_SAFE (newer
        # Smart Wallet) accounts query the correct funder address. Default
        # (signature_type=0) preserves EOA behavior for direct on-chain users.
        sig_type, funder = _validate_wallet_config(
            getattr(config, "poly_signature_type", 0),
            getattr(config, "poly_funder", ""),
        )
        self._client = _make_clob_client(
            key=key, creds=creds, signature_type=sig_type, funder=funder,
        )
        self._signature_type = sig_type
        self._funder = funder
        self._timeout = 30
        if sig_type != 0:
            logger.info(
                "OrderClient: signature_type=%d (%s) funder=%s",
                sig_type, _SIGNATURE_TYPE_LABELS[sig_type], funder or "(auto-derived)",
            )
        if sig_type == 1 and funder:
            logger.warning(
                "OrderClient: POLY_SIGNATURE_TYPE=1 with POLY_FUNDER set. "
                "If this is a Polymarket deposit wallet, use "
                "POLY_SIGNATURE_TYPE=3 (POLY_1271) instead."
            )

    def _with_timeout(self, fn, *args, **kwargs):
        """Run a CLOB call with timeout to prevent thread-pool starvation."""
        future = _clob_executor.submit(fn, *args, **kwargs)
        return future.result(timeout=self._timeout)

    def fetch_order_book(self, token_id: str) -> dict | None:
        """Fetch order book for a single token. Returns None on error."""
        return _fetch_order_book(self._timeout, token_id)

    def best_bid(self, book: dict) -> tuple[float, float] | None:
        """Extract best bid (price, size) from order book.

        Note: py-clob-client sorts bids ascending — best bid is LAST element.
        """
        return _best_bid(book)

    def best_ask(self, book: dict) -> tuple[float, float] | None:
        """Extract best ask (lowest price = best for buyer) from order book.

        Polymarket `/book` asks are not guaranteed to already be ascending.
        """
        return _best_ask(book)

    def check_balance(self) -> float | None:
        """Get pUSD (V2 collateral) balance in dollars. Returns None on API failure."""
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self._with_timeout(self._client.get_balance_allowance, params)
            return int(result.get("balance", "0")) / 1e6
        except FuturesTimeoutError:
            logger.error("Balance check timed out after %ds", self._timeout)
            return None
        except Exception:
            logger.error("Failed to check CLOB balance", exc_info=True)
            return None

    def sync_collateral_balance(self) -> bool:
        """Ask CLOB to refresh its cached pUSD collateral balance."""
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            self._with_timeout(self._client.update_balance_allowance, params)
            return True
        except FuturesTimeoutError:
            logger.error("Collateral balance sync timed out after %ds", self._timeout)
            return False
        except Exception:
            logger.error("Failed to sync CLOB collateral balance", exc_info=True)
            return False

    def _refresh_conditional_allowance(
        self,
        token_id: str,
        *,
        min_size: float | None = None,
    ) -> str | None:
        """Refresh and verify CLOB allowance for a conditional-token sell."""
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
            )
            self._with_timeout(self._client.update_balance_allowance, params)
            if min_size is not None and math.isfinite(min_size) and min_size > 0:
                state = self._with_timeout(self._client.get_balance_allowance, params)
                required_units = int(math.ceil(min_size * 1_000_000 - 1e-6))
                try:
                    balance_units = int(str(state.get("balance", "0")))
                except (AttributeError, TypeError, ValueError):
                    return "conditional balance payload missing balance"
                if balance_units < required_units:
                    return (
                        "conditional token balance below close size: "
                        f"balance_units={balance_units} required_units={required_units}"
                    )
                allowances = state.get("allowances") if isinstance(state, dict) else None
                if isinstance(allowances, dict) and allowances:
                    missing: list[str] = []
                    for spender, raw_value in allowances.items():
                        try:
                            allowance_units = int(str(raw_value))
                        except (TypeError, ValueError):
                            missing.append(str(spender))
                            continue
                        if allowance_units < required_units:
                            missing.append(str(spender))
                    if missing:
                        return (
                            "conditional token allowance below close size for spender(s): "
                            + ", ".join(missing)
                        )
            return None
        except FuturesTimeoutError:
            logger.error(
                "Conditional-token allowance refresh timed out after %ds for %s",
                self._timeout,
                token_id,
            )
            return "conditional allowance refresh timed out"
        except Exception as exc:
            logger.error(
                "Failed to refresh conditional-token allowance for %s: %s",
                token_id,
                exc,
                exc_info=True,
            )
            return str(exc)

    def place_order(self, signal: BetSignal) -> OrderResult:
        """Place a FAK limit order for the given signal.

        Uses limit_price (highest walk-the-book level) to ensure fill across
        multiple ask levels. Any unfilled remainder is cancelled by CLOB and
        later scanner ticks may top up the slot from fresh price/edge state.
        """
        try:
            from py_clob_client_v2.order_builder.constants import BUY
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            from hightempbot.execution.strategy_constants import MIN_BET_USD

            # Use limit_price (walked) if available, else fill_price (best_ask)
            order_price = signal.limit_price if signal.limit_price > 0 else signal.fill_price
            if not (math.isfinite(order_price) and order_price > 0):
                return OrderResult(error="order_price <= 0", error_kind="invalid_amounts")

            # Size from the executable limit price so the order cannot spend
            # more than the capped stake if the cheaper levels disappear. CLOB
            # market BUYs reject maker collateral with sub-cent precision, so
            # quantize on the signed-order grid before create_order/post_order.
            quantized = _quantize_market_buy_size(
                signal.bet_size_usd,
                order_price,
                min_bet_usd=MIN_BET_USD,
            )
            if quantized is None:
                return OrderResult(
                    error=(
                        "market buy amount below CLOB precision/minimum after quantization: "
                        f"target_usd={signal.bet_size_usd:.8f} price={order_price:.8f}"
                    ),
                    error_kind="invalid_amounts",
                )
            size, max_spend_usd = quantized
            if max_spend_usd + 1e-9 < signal.bet_size_usd:
                logger.debug(
                    "Quantized market buy for %s: target_usd=%.8f price=%.8f "
                    "size=%.8f max_spend_usd=%.2f",
                    signal.bracket_label,
                    signal.bet_size_usd,
                    order_price,
                    size,
                    max_spend_usd,
                )

            order_args = OrderArgs(
                price=order_price,
                size=size,
                side=BUY,
                token_id=signal.token_id,
            )

            signed_order = self._with_timeout(self._client.create_order, order_args)
            order_shape_error = _validate_signed_order_for_wallet(
                signed_order,
                signature_type=getattr(self, "_signature_type", 0),
                funder=getattr(self, "_funder", ""),
            )
            if order_shape_error:
                logger.error("Refusing to post malformed CLOB order: %s", order_shape_error)
                return OrderResult(error=order_shape_error, error_kind="auth")
            amount_shape_error = _validate_market_buy_amount_precision(signed_order)
            if amount_shape_error:
                logger.error("Refusing to post malformed CLOB order: %s", amount_shape_error)
                return OrderResult(error=amount_shape_error, error_kind="invalid_amounts")
            response = self._with_timeout(self._client.post_order, signed_order, OrderType.FAK)

            order_id = response.get("orderID") or response.get("order_id")
            fill_ts = utc_now_sql()

            return OrderResult(
                order_id=order_id,
                limit_price=order_price,
                # Until verification can read the actual matched fill, record
                # the conservative limit-price notional. execute_or_log
                # overwrites this with matched price/size when CLOB exposes it.
                fill_price=order_price,
                fill_size=size,
                fill_ts=fill_ts,
                success=True,
                bet_size_usd=max_spend_usd,
            )
        except Exception as e:
            logger.error("Order placement failed for %s: %s", signal.bracket_label, e, exc_info=True)
            return OrderResult(error=str(e), error_kind=_classify_clob_error(e))

    def close_position(
        self,
        token_id: str,
        *,
        target_size: float,
        min_acceptable_vwap: float | None = None,
        max_acceptable_vwap: float | None = None,
    ) -> OrderResult:
        """Sell the full ``target_size`` at the realised bid VWAP.

        Walks the bid book to compute ``(fillable_size, vwap, worst_price)``
        for the requested size, then submits an all-or-nothing sell at
        ``worst_price`` so wide spreads aren't rounded up to a single best-bid
        number and the ledger never records a partial exit as closed. The
        actual matched price/size is read back from the response when
        present; otherwise the pre-trade VWAP/target size is recorded. Optional
        VWAP bounds let TP/SL callers abort if a fresh quote no longer
        satisfies the trigger that caused the close.
        """
        try:
            try:
                from py_clob_client_v2.order_builder.constants import SELL
            except ImportError:
                SELL = "SELL"
            from py_clob_client_v2.clob_types import OrderArgs, OrderType

            if not token_id:
                return OrderResult(error="token_id required")
            if not (math.isfinite(target_size) and target_size > 0):
                return OrderResult(error="close size <= 0")

            book = self.fetch_order_book(token_id)
            if not book:
                return OrderResult(error="failed to fetch order book")

            quote, quote_error = _quote_close_sell_with_error(book, target_size)
            if quote is None:
                return OrderResult(error=quote_error or "no executable bids")
            _fillable_size, vwap, worst_price = quote
            if min_acceptable_vwap is not None:
                min_vwap = float(min_acceptable_vwap)
                if math.isfinite(min_vwap) and vwap + 1e-9 < min_vwap:
                    return OrderResult(
                        error=(
                            "close vwap below minimum acceptable trigger: "
                            f"vwap={vwap:.8f} minimum={min_vwap:.8f}"
                        ),
                        error_kind="stale_quote",
                    )
            if max_acceptable_vwap is not None:
                max_vwap = float(max_acceptable_vwap)
                if math.isfinite(max_vwap) and vwap - 1e-9 > max_vwap:
                    return OrderResult(
                        error=(
                            "close vwap above maximum acceptable trigger: "
                            f"vwap={vwap:.8f} maximum={max_vwap:.8f}"
                        ),
                        error_kind="stale_quote",
                    )
            allowance_error = self._refresh_conditional_allowance(
                token_id,
                min_size=target_size,
            )
            if allowance_error:
                return OrderResult(
                    error=f"conditional allowance refresh failed: {allowance_error}",
                    error_kind="auth",
                )

            order_args = OrderArgs(
                price=worst_price,
                size=target_size,
                side=SELL,
                token_id=token_id,
            )
            signed_order = self._with_timeout(self._client.create_order, order_args)
            order_shape_error = _validate_signed_order_for_wallet(
                signed_order,
                signature_type=getattr(self, "_signature_type", 0),
                funder=getattr(self, "_funder", ""),
            )
            if order_shape_error:
                logger.error("Refusing to post malformed CLOB close order: %s", order_shape_error)
                return OrderResult(error=order_shape_error, error_kind="auth")
            response = self._with_timeout(self._client.post_order, signed_order, OrderType.FOK)
            order_id = response.get("orderID") or response.get("order_id")
            if response.get("success") is False or response.get("error"):
                close_error = response.get("error") or "close order rejected"
                return OrderResult(
                    error=close_error,
                    error_kind=_classify_clob_error(close_error),
                )
            if not order_id:
                return OrderResult(
                    error="close order returned no order_id",
                    error_kind="unknown",
                )

            actual_price, actual_size = _extract_close_fill(response)
            if actual_size is not None and not close_size_fills_target(actual_size, target_size):
                return OrderResult(
                    error=(
                        "close order filled only part of position: "
                        f"filled={actual_size:.8f} target={target_size:.8f}"
                    )
                )
            fill_price = actual_price if actual_price is not None else vwap
            fill_size = min(actual_size, target_size) if actual_size is not None else target_size

            return OrderResult(
                order_id=order_id,
                limit_price=worst_price,
                fill_price=float(fill_price),
                fill_size=float(fill_size),
                fill_ts=utc_now_sql(),
                success=True,
            )
        except Exception as e:
            logger.error("Close order failed for token %s: %s", token_id, e, exc_info=True)
            return OrderResult(error=str(e), error_kind=_classify_clob_error(e))


def _prob_safe_floor(signal: BetSignal) -> float:
    """Side-aware LUT-calibrated probability, falling back to ``p_model`` when absent.

    YES: bucket observed rate. NO: 1 - bucket observed rate.
    Decision pipeline pre-computes this on ``BetSignal.prob_safe_floor`` so the
    walker and verify-loop can read it without recomputing.
    """
    if signal.prob_safe_floor is not None:
        return signal.prob_safe_floor
    if signal.side == "YES":
        return signal.p_model
    return 1.0 - signal.p_model


def _find_open_matching_order(
    order_client: OrderClient,
    token_id: str,
    side: str,
    *,
    bot_placed_ids: set[str],
) -> str | None:
    """Return id of an open order this bot placed for ``(token_id, side)``.

    Used on retry attempts to avoid double-submitting when attempt N-1 landed
    on CLOB but the verify step failed — place_order is not idempotent.

    ``bot_placed_ids`` scopes adoption to orders this bot itself submitted
    earlier in the current ``execute_or_log`` retry chain. Without this scope
    a manual order on the Polymarket account matching (token_id, side) would
    be silently adopted as the bot's fill (ce-code-review P0 #2 / ADV-002).
    """
    try:
        orders = _bounded_client_call(order_client, order_client._client.get_open_orders) or []
    except FuturesTimeoutError:
        logger.warning("get_open_orders timed out during retry idempotency check")
        return None
    except Exception:
        logger.debug("get_open_orders raised during retry idempotency check", exc_info=True)
        return None
    # CLOB orders are always BUY orders against a side-specific token. The
    # YES/NO exposure is encoded by ``token_id``, so accept BUY here as well
    # as legacy/test payloads that store the logical side.
    side_up = side.upper()
    accepted_sides = {side_up, "BUY", ""}
    for o in orders or []:
        if not isinstance(o, dict):
            o = getattr(o, "__dict__", {}) or {}
        o_token = o.get("asset_id") or o.get("token_id")
        o_side = (o.get("side") or "").upper()
        if o_token != token_id or o_side not in accepted_sides:
            continue
        candidate_id = o.get("id") or o.get("order_id")
        if not candidate_id:
            continue
        if candidate_id not in bot_placed_ids:
            # Foreign open order on the same token+side — refuse to adopt.
            logger.warning(
                "Refusing to adopt foreign open order %s on token %s side %s "
                "(not in bot-placed set; possibly a manual order on the same account)",
                candidate_id, token_id, side,
            )
            continue
        return candidate_id
    return None


def _cancel_open_order(order_client: OrderClient, order_id: str) -> bool:
    """Best-effort cancel for an unverified live order.

    Returns True only when the order is known not to be live anymore. If the
    order appears matched/filled or the cancel call fails, callers must keep
    the ledger row PENDING with its order_id so reconciliation can see it.
    """
    if not order_id:
        return True

    try:
        raw = _bounded_client_call(order_client, order_client._client.get_order, order_id)
        status = (raw.get("status") if isinstance(raw, dict) else getattr(raw, "status", "")) or ""
        status_up = str(status).upper()
        if status_up in _MATCHED_ORDER_STATUSES:
            return False
        if status_up in _TERMINAL_ORDER_STATUSES:
            return True
    except FuturesTimeoutError:
        logger.warning("cancel pre-check get_order timed out for %s", order_id)
    except Exception:
        logger.debug("cancel pre-check get_order failed for %s", order_id, exc_info=True)

    try:
        from py_clob_client_v2.clob_types import OrderPayload
        response = _bounded_client_call(
            order_client, order_client._client.cancel_order, OrderPayload(orderID=order_id)
        )
    except FuturesTimeoutError:
        logger.warning("Cancel timed out for unverified order %s", order_id)
        return False
    except Exception:
        logger.warning("Failed to cancel unverified order %s", order_id, exc_info=True)
        return False

    if isinstance(response, dict):
        if response.get("success") is False or response.get("error"):
            logger.warning("Cancel rejected for unverified order %s: %s", order_id, response)
            return False
        not_canceled = response.get("not_canceled")
        if isinstance(not_canceled, dict) and not_canceled:
            logger.warning("Cancel rejected for unverified order %s: %s", order_id, response)
            return False
        canceled = response.get("canceled")
        if isinstance(canceled, list):
            return order_id in canceled
        if isinstance(canceled, str):
            return canceled == order_id
    return True


def _write_pipeline_health_error(
    conn: sqlite3.Connection,
    station_id: str,
    bracket_label: str,
    max_retries: int,
    reason: str | None = None,
    status: str = "ERROR",
) -> None:
    from hightempbot.db.connection import log_pipeline_health
    suffix = f" ({reason})" if reason else ""
    if status == "WARNING" and reason == "no_tx_hash":
        message = (
            f"{station_id}/{bracket_label}: matched but tx hash pending "
            f"after {max_retries} verify attempts"
        )
    else:
        message = f"{station_id}/{bracket_label}: {max_retries}x failed{suffix}"
    log_pipeline_health(
        conn, station_id, "order", status, message,
    )


def _safe_send_alert(
    config: Config | None,
    station_id: str,
    bracket_label: str,
    side: str,
    attempts: int,
    reason: str,
    stage: str,
) -> None:
    """Fire one operational Telegram alert. Swallows all exceptions.

    Narrow payload: station, bracket, side, attempts, reason category,
    timestamp. Never includes probabilities, LCB/UCB, VWAP, edge, bet size,
    order IDs, or credential material.
    """
    if config is None:
        return
    if stage == "verification_downgraded":
        return
    try:
        from hightempbot.execution.notify import format_alert_text, send_alert
        alert_bracket = format_alert_text(bracket_label)
        if stage == "order_terminal_fail":
            title = f"[hightempbot] Bet submit blocked: {station_id}"
        else:
            title = f"[hightempbot] Bet retry exhausted: {station_id}"
        message = (
            f"station={station_id} bracket={alert_bracket} side={side} "
            f"attempts={attempts} reason={reason} time={utc_now_sql()}"
        )
        send_alert(
            title=title,
            message=message,
            config=config,
            stage=stage,
            station_id=station_id,
        )
    except Exception:
        logger.error("send_alert raised — swallowed to preserve cancellation path", exc_info=True)


def _dry_run_result(signal: BetSignal, order_client: OrderClient | None) -> OrderResult:
    """Build a realistic dry-run OrderResult by walking the live book.

    Stamps ``transaction_hash='DRY_RUN_<uuid>'`` and ``verify_attempts=0`` so
    dry-run PnL is comparable to backtest per the LCB plan's dry-run realism
    spec. Falls back to the signal's stored fill_price if the book fetch fails
    or there are no valid asks.
    """
    from hightempbot.execution.strategy_constants import MIN_BET_USD, MIN_EDGE, POLY_FEE_THETA

    fill_price = signal.fill_price
    fill_size = (signal.bet_size_usd / fill_price) if fill_price > 0 else 0.0
    realized_edge = signal.edge
    fill_levels: list[dict[str, float]] = []

    if order_client is not None:
        try:
            book = order_client.fetch_order_book(signal.token_id)
        except Exception:
            book = None
        if book is not None:
            walked = walk_book_edge_preserving(
                book,
                signal.bet_size_usd,
                prob_safe_floor=_prob_safe_floor(signal),
                fee_theta=POLY_FEE_THETA,
                min_edge=_min_edge_for_signal(signal, fallback=MIN_EDGE),
                min_bet_usd=MIN_BET_USD,
                max_walk_price=_max_walk_for_signal(signal),
                walk_anchor_price=_walk_anchor_for_signal(signal),
                return_levels=True,
            )
            if walked is not None:
                filled_usd, filled_shares, filled_vwap, _limit, walked_edge, fill_levels = walked
                fill_price = filled_vwap
                fill_size = filled_shares
                realized_edge = walked_edge
    if not fill_levels and fill_price > 0 and fill_size > 0:
        fill_levels = [{"price": fill_price, "shares": fill_size, "usd": fill_price * fill_size}]

    return OrderResult(
        fill_price=fill_price,
        fill_size=fill_size,
        fill_ts=utc_now_sql(),
        success=False,  # dry-run never counts as a live fill
        realized_edge=realized_edge,
        transaction_hash=f"DRY_RUN_{uuid.uuid4().hex[:16]}",
        verify_attempts=0,
        fill_levels=fill_levels,
    )


def _finalize_verified_match(
    signal: BetSignal,
    *,
    verify_order_id: str,
    order_client: OrderClient,
    submit_result: OrderResult | None,
    fill_limit: float | None,
    realized_edge: float | None,
    fill_levels_fallback: list[dict[str, float]] | None,
    tx_hash: str,
    attempt: int,
    conn: sqlite3.Connection | None,
) -> OrderResult:
    """Build the success OrderResult once verify confirmed MATCHED + tx_hash.

    Re-fetches trades for forensic fill levels; falls back to get_order
    extraction if get_trades is silent. When neither path reports a
    positive fill, returns a leave_pending OrderResult so reconciliation
    can keep the row PENDING rather than booking a zero-fill win.
    """
    from hightempbot.execution.strategy_constants import POLY_FEE_THETA
    real_price: float | None = None
    real_size: float | None = None
    real_levels: list[dict[str, float]] = []
    try:
        raw_trades = _get_trades_for_order(
            order_client,
            verify_order_id,
            market_id=signal.market_id,
            token_id=signal.token_id,
        )
        real_levels = _fill_levels_from_trades(raw_trades)
        if real_levels:
            real_notional = sum(level["usd"] for level in real_levels)
            real_shares = sum(level["shares"] for level in real_levels)
            if real_shares > 0:
                real_price = real_notional / real_shares
                real_size = real_shares
    except Exception:
        logger.debug("get_trades fill extraction failed for %s", verify_order_id, exc_info=True)
    try:
        raw = _bounded_client_call(
            order_client, order_client._client.get_order, verify_order_id
        )
        order_price, order_size = _extract_matched_fill(raw)
        if real_price is None:
            real_price = order_price
        if real_size is None:
            real_size = order_size
    except Exception:
        logger.debug("get_order fill extraction failed for %s", verify_order_id, exc_info=True)

    if not _has_positive_fill(real_price, real_size):
        if conn is not None:
            _write_pipeline_health_error(
                conn, signal.station_id, signal.bracket_label,
                attempt, reason="fill_unknown", status="WARNING",
            )
        return OrderResult(
            order_id=verify_order_id,
            limit_price=fill_limit,
            error="verified_fill_unknown",
            success=False,
            transaction_hash=tx_hash,
            verify_attempts=attempt,
            leave_pending=True,
        )

    result = submit_result or OrderResult(order_id=verify_order_id)
    result.order_id = verify_order_id
    result.limit_price = result.limit_price if result.limit_price is not None else fill_limit
    result.fill_price = real_price
    result.fill_size = real_size
    result.fill_ts = result.fill_ts or utc_now_sql()
    result.success = True
    result.bet_size_usd = float(result.fill_price) * float(result.fill_size)
    result.realized_edge = _realized_edge_for_fill(
        signal, result.fill_price, fallback=realized_edge, fee_theta=POLY_FEE_THETA
    )
    result.transaction_hash = tx_hash
    result.verify_attempts = attempt
    result.verification_downgraded = False
    result.fill_levels = real_levels or fill_levels_fallback
    return result


def _finalize_observable_fill(
    signal: BetSignal,
    *,
    submit_result: OrderResult | None,
    verify_order_id: str,
    fill_limit: float | None,
    realized_edge: float | None,
    recorded_price: float,
    recorded_shares: float,
    recorded_levels: list[dict[str, float]],
    snapshot: _OrderExecutionSnapshot,
    attempt: int,
    conn: sqlite3.Connection | None,
) -> OrderResult:
    """Build the success OrderResult for a partial fill observed in trades but
    not yet confirmed on-chain.

    Flags ``verification_downgraded`` when no transaction_hash arrived so
    the dashboard records the weaker proof.
    """
    from hightempbot.execution.strategy_constants import POLY_FEE_THETA
    result = submit_result or OrderResult(
        order_id=verify_order_id,
        limit_price=fill_limit,
        fill_ts=utc_now_sql(),
        success=True,
    )
    result.order_id = verify_order_id
    result.limit_price = result.limit_price if result.limit_price is not None else fill_limit
    result.fill_price = float(recorded_price)
    result.fill_size = float(recorded_shares)
    result.fill_ts = result.fill_ts or utc_now_sql()
    result.success = True
    result.bet_size_usd = float(recorded_price) * float(recorded_shares)
    result.realized_edge = _realized_edge_for_fill(
        signal, result.fill_price, fallback=realized_edge, fee_theta=POLY_FEE_THETA
    )
    result.transaction_hash = snapshot.transaction_hash
    result.verify_attempts = attempt
    result.verification_downgraded = snapshot.transaction_hash is None
    result.fill_levels = recorded_levels or _fill_levels_from_price_size(
        result.fill_price, result.fill_size
    )
    if result.verification_downgraded and conn is not None:
        _write_pipeline_health_error(
            conn, signal.station_id, signal.bracket_label,
            attempt, reason="no_tx_hash", status="WARNING",
        )
    return result


def _resolve_terminal_state(
    signal: BetSignal,
    order_client: OrderClient,
    *,
    last_result: OrderResult | None,
    last_reason: str,
    any_matched_no_tx: bool,
    conn: sqlite3.Connection | None,
    config: Config | None,
) -> OrderResult:
    """Decide the OrderResult after the retry loop has run to exhaustion.

    Three outcomes:
      1. ``any_matched_no_tx`` set → MATCHED was seen but on-chain proof
         never arrived. Returns a verification_downgraded result (either
         "matched_fill_unknown" leave_pending or a success flagged as
         degraded), depending on whether a positive fill was recorded.
      2. ``last_result.leave_pending`` set → keep PENDING for the
         reconciliation pass to settle.
      3. Otherwise → CANCELLED. Attempts to cancel any still-open order;
         if the cancel fails, returns a leave_pending result so the
         operator alert + reconciliation see the open order_id.
    """
    attempts = (
        int(last_result.verify_attempts)
        if last_result is not None and last_result.verify_attempts
        else MAX_ORDER_RETRIES
    )

    def _health(reason: str, status: str = "ERROR") -> None:
        """Bind the (conn, station, bracket, attempts) tuple shared across the 4 health writes in this resolver."""
        if conn is not None:
            _write_pipeline_health_error(
                conn, signal.station_id, signal.bracket_label,
                attempts, reason=reason, status=status,
            )

    if any_matched_no_tx and last_result is not None:
        if not _has_positive_fill(last_result.fill_price, last_result.fill_size):
            _health("fill_unknown", status="WARNING")
            return OrderResult(
                order_id=last_result.order_id,
                limit_price=last_result.limit_price,
                error="matched_fill_unknown",
                success=False,
                verify_attempts=attempts,
                verification_downgraded=True,
                leave_pending=True,
            )
        # Treat as FILLED but flag: on-chain proof never materialized.
        _health("no_tx_hash", status="WARNING")
        return OrderResult(
            order_id=last_result.order_id,
            limit_price=last_result.limit_price,
            fill_price=last_result.fill_price,
            fill_size=last_result.fill_size,
            fill_ts=last_result.fill_ts or utc_now_sql(),
            success=True,
            bet_size_usd=(
                float(last_result.fill_price) * float(last_result.fill_size)
                if last_result.fill_price and last_result.fill_size
                else None
            ),
            realized_edge=last_result.realized_edge,
            transaction_hash=None,
            verify_attempts=attempts,
            verification_downgraded=True,
            fill_levels=last_result.fill_levels,
        )

    if last_result is not None and last_result.leave_pending:
        last_result.verify_attempts = attempts
        _health(last_reason, status="WARNING")
        return last_result

    # CANCELLED — no successful verified fill.
    open_order_id = last_result.order_id if last_result is not None else None
    if open_order_id:
        cancel_ok = _cancel_open_order(order_client, open_order_id)
        if not cancel_ok:
            _safe_send_alert(
                config, signal.station_id, signal.bracket_label, signal.side,
                attempts, "cancel_failed", stage="order_cancel_fail",
            )
            _health("cancel_failed")
            return OrderResult(
                order_id=open_order_id,
                limit_price=last_result.limit_price if last_result is not None else None,
                error=f"cancel_failed_after_verify: {last_reason}",
                success=False,
                verify_attempts=attempts,
                leave_pending=True,
            )

    terminal_failure = (
        last_result is not None
        and last_result.error_kind in _TERMINAL_ERROR_KINDS
    )
    alert_stage = "order_terminal_fail" if terminal_failure else "order_retry_fail"

    _safe_send_alert(
        config, signal.station_id, signal.bracket_label, signal.side,
        attempts, last_reason, stage=alert_stage,
    )
    _health(last_reason)
    error = last_result.error if last_result else last_reason
    if terminal_failure and last_result is not None and last_result.error_kind:
        error = f"{last_result.error_kind}: {error}"
    return OrderResult(
        order_id=open_order_id,
        limit_price=last_result.limit_price if last_result is not None else None,
        error=error,
        error_kind=last_result.error_kind if last_result is not None else None,
        verify_attempts=attempts,
    )


def execute_or_log(
    signal: BetSignal,
    order_client: OrderClient | None,
    dry_run: bool,
    row_id: int | None = None,
    conn: sqlite3.Connection | None = None,
    config: Config | None = None,
) -> OrderResult | None:
    """Execute a bet signal and finalize the pre-inserted pending ledger row.

    Full-feature path (row_id + conn provided): posts an edge-preserving FAK
    buy. If the FAK partially fills, only the actual notional is persisted and
    later scanner ticks can top up the slot. If a posted order's final state is
    unclear, retries verify the same order instead of submitting another order
    inside the tick.

    Dry-run full-feature path: simulate a realistic fill via the same walker,
    stamp ``DRY_RUN_<uuid>`` on the row, skip the verify loop.

    Legacy path (row_id or conn omitted): dry-run callers still get log-only
    behavior. Live callers are refused because they must flow through the pipeline's
    PENDING-first ledger row for reconciliation and operator visibility.
    """
    # --- Legacy path: preserves the pre-Unit-6 3-arg signature ---
    if row_id is None or conn is None:
        if dry_run:
            logger.info(
                "DRY-RUN: would bet %s %.2f USD at %.3f (edge=%.3f)",
                signal.bracket_label, signal.bet_size_usd,
                signal.fill_price, signal.edge,
            )
            return None
        logger.error("Live execution refused without a pre-inserted pending ledger row")
        return OrderResult(
            error="live execution requires pre-inserted pending ledger row",
            error_kind="unknown",
        )

    # --- Full-feature path: owns the ledger PENDING → FILLED/CANCELLED UPDATE ---
    def _finish(result: OrderResult | None) -> OrderResult | None:
        update_pending_bet_after_execution(
            conn,
            row_id,
            dry_run=dry_run,
            order_result=result,
        )
        return result

    if dry_run:
        logger.info(
            "DRY-RUN: would bet %s %.2f USD at %.3f (edge=%.3f)",
            signal.bracket_label,
            signal.bet_size_usd,
            signal.fill_price,
            signal.edge,
        )
        return _finish(_dry_run_result(signal, order_client))

    if order_client is None:
        logger.error("Live mode but no order_client configured")
        return _finish(OrderResult(error="no order_client"))

    from hightempbot.execution.strategy_constants import (
        MIN_BET_USD, MIN_EDGE, ORDER_RETRY_BACKOFF_S,
        ORDER_VERIFY_POLL_S, POLY_FEE_THETA, VERIFY_POLY_TIMEOUT_S,
    )
    from hightempbot.persistence.reconciliation import verify_order_matched

    prob_floor = _prob_safe_floor(signal)
    last_reason = "unknown"
    any_matched_no_tx = False
    matched_order_id: str | None = None
    pending_order_id: str | None = None
    last_result: OrderResult | None = None
    # Track order_ids the bot itself submits during this retry chain so
    # _find_open_matching_order refuses to adopt foreign open orders on the
    # same token+side (ce-code-review P0 #2 / ADV-002). Reset per call so
    # cross-signal retries cannot leak state.
    bot_placed_ids: set[str] = set()

    for attempt in range(1, MAX_ORDER_RETRIES + 1):
        submit_result: OrderResult | None = None

        if matched_order_id is not None and last_result is not None:
            # The previous attempt reached MATCHED/FILLED but the tx hash had
            # not propagated yet. Never submit another order for that signal;
            # only keep verifying the already-matched order.
            verify_order_id = matched_order_id
            filled_vwap = last_result.fill_price if last_result.fill_price is not None else signal.fill_price
            filled_shares = last_result.fill_size if last_result.fill_size is not None else 0.0
            fill_limit = last_result.limit_price if last_result.limit_price is not None else signal.limit_price
            fill_levels = last_result.fill_levels
            filled_usd = (
                float(filled_vwap) * float(filled_shares)
                if filled_vwap is not None and filled_shares is not None
                else signal.bet_size_usd
            )
            realized_edge = last_result.realized_edge if last_result.realized_edge is not None else signal.edge
        elif pending_order_id is not None and last_result is not None:
            # A FAK order was accepted by CLOB but neither a positive fill nor
            # a terminal no-fill state was observable yet. Verify that same
            # order only; do not submit another market attempt in this tick.
            verify_order_id = pending_order_id
            filled_vwap = last_result.fill_price if last_result.fill_price is not None else signal.fill_price
            filled_shares = last_result.fill_size if last_result.fill_size is not None else 0.0
            fill_limit = last_result.limit_price if last_result.limit_price is not None else signal.limit_price
            fill_levels = last_result.fill_levels
            filled_usd = (
                float(filled_vwap) * float(filled_shares)
                if filled_vwap is not None and filled_shares is not None
                else signal.bet_size_usd
            )
            realized_edge = last_result.realized_edge if last_result.realized_edge is not None else signal.edge
        else:
            # Re-fetch + re-walk at fresh book for every fresh submission attempt.
            book = order_client.fetch_order_book(signal.token_id)
            if book is None:
                last_reason = "book_fetch_failed"
                last_result = OrderResult(error="failed to fetch order book", verify_attempts=attempt)
                if attempt < MAX_ORDER_RETRIES:
                    time.sleep(ORDER_RETRY_BACKOFF_S)
                continue

            # Cap retry target by the first walk's proven depth.
            target_usd = signal.bet_size_usd
            if last_result is not None and last_result.fill_size and last_result.fill_price:
                prior_usd = float(last_result.fill_price) * float(last_result.fill_size)
                target_usd = min(target_usd, prior_usd)

            walked = walk_book_edge_preserving(
                book,
                target_usd,
                prob_safe_floor=prob_floor,
                fee_theta=POLY_FEE_THETA,
                min_edge=_min_edge_for_signal(signal, fallback=MIN_EDGE),
                min_bet_usd=MIN_BET_USD,
                max_walk_price=_max_walk_for_signal(signal),
                walk_anchor_price=_walk_anchor_for_signal(signal),
                return_levels=True,
            )
            if walked is None:
                last_reason = "price_moved"
                last_result = OrderResult(error="insufficient_depth", verify_attempts=attempt)
                return _finish(last_result)

            filled_usd, filled_shares, filled_vwap, fill_limit, realized_edge, fill_levels = walked

            # Retry idempotency: reuse an open order matching this token before
            # submitting a fresh one. place_order is not idempotent.
            # Scoped to bot_placed_ids — manual orders on the same account
            # cannot be adopted as the bot's fill.
            existing_order_id = None
            if attempt > 1 and bot_placed_ids:
                existing_order_id = _find_open_matching_order(
                    order_client, signal.token_id, signal.side,
                    bot_placed_ids=bot_placed_ids,
                )

            if existing_order_id is not None:
                verify_order_id = existing_order_id
            else:
                order_signal = replace(
                    signal,
                    fill_price=filled_vwap,
                    limit_price=fill_limit,
                    edge=realized_edge,
                    bet_size_usd=filled_usd,
                    gate_results=dict(signal.gate_results),
                )
                try:
                    submit_result = order_client.place_order(order_signal)
                except Exception as e:
                    logger.error("place_order raised on attempt %d: %s", attempt, e, exc_info=True)
                    kind = _classify_clob_error(e)
                    last_reason = kind if kind in _TERMINAL_ERROR_KINDS else "api_error"
                    last_result = OrderResult(
                        error=str(e), error_kind=kind, verify_attempts=attempt,
                    )
                    if kind in _TERMINAL_ERROR_KINDS:
                        # Non-retryable: stop burning the retry budget on a
                        # condition retries cannot fix (ce-code-review P1 #13).
                        logger.error(
                            "place_order terminal error_kind=%s on attempt %d "
                            "for %s -- aborting retry loop",
                            kind, attempt, signal.bracket_label,
                        )
                        break
                    if attempt < MAX_ORDER_RETRIES:
                        time.sleep(ORDER_RETRY_BACKOFF_S)
                    continue

                if not submit_result.success or not submit_result.order_id:
                    kind = submit_result.error_kind or _classify_clob_error(submit_result.error)
                    last_reason = kind if kind in _TERMINAL_ERROR_KINDS else "api_error"
                    last_result = OrderResult(
                        error=submit_result.error or "place_order returned no order_id",
                        error_kind=kind,
                        verify_attempts=attempt,
                    )
                    if kind in _TERMINAL_ERROR_KINDS:
                        logger.error(
                            "place_order returned terminal error_kind=%s on attempt %d "
                            "for %s -- aborting retry loop",
                            kind, attempt, signal.bracket_label,
                        )
                        break
                    if attempt < MAX_ORDER_RETRIES:
                        time.sleep(ORDER_RETRY_BACKOFF_S)
                    continue
                submit_result.fill_levels = fill_levels
                verify_order_id = submit_result.order_id
                # Record this bot-placed order_id so subsequent retries can
                # adopt it but cannot adopt foreign orders on the same token.
                if verify_order_id:
                    bot_placed_ids.add(str(verify_order_id))

        # Step 2: verify via CLOB poll. Sets tx_hash if chain proof lands.
        verified, tx_hash = verify_order_matched(
            order_client,
            verify_order_id,
            poll_interval_s=ORDER_VERIFY_POLL_S,
            timeout_s=VERIFY_POLY_TIMEOUT_S,
        )

        if verified and tx_hash:
            # Success: MATCHED + on-chain proof. Finalize with forensics.
            return _finish(_finalize_verified_match(
                signal,
                verify_order_id=verify_order_id,
                order_client=order_client,
                submit_result=submit_result,
                fill_limit=fill_limit,
                realized_edge=realized_edge,
                fill_levels_fallback=fill_levels,
                tx_hash=tx_hash,
                attempt=attempt,
                conn=conn,
            ))

        # Not verified. A FAK order may end terminal after a partial fill, so
        # read actual trade/order fill data before deciding it was a no-fill.
        snapshot = _read_order_execution_snapshot(order_client, verify_order_id)
        recorded_price = snapshot.fill_price
        recorded_shares = snapshot.fill_size
        recorded_levels = snapshot.fill_levels or []
        has_positive_fill = _has_positive_fill(recorded_price, recorded_shares)

        if has_positive_fill:
            return _finish(_finalize_observable_fill(
                signal,
                submit_result=submit_result,
                verify_order_id=verify_order_id,
                fill_limit=fill_limit,
                realized_edge=realized_edge,
                recorded_price=recorded_price,
                recorded_shares=recorded_shares,
                recorded_levels=recorded_levels,
                snapshot=snapshot,
                attempt=attempt,
                conn=conn,
            ))

        status_up = snapshot.status
        if status_up in _MATCHED_ORDER_STATUSES:
            any_matched_no_tx = True
            matched_order_id = verify_order_id
            pending_order_id = None
            last_reason = "no_tx_hash"
        elif status_up in _TERMINAL_ORDER_STATUSES and snapshot.trades_unknown:
            pending_order_id = verify_order_id
            last_reason = "terminal_trades_unknown"
            last_result = OrderResult(
                order_id=verify_order_id,
                limit_price=fill_limit,
                error=f"verify_failed: {last_reason}",
                verify_attempts=attempt,
                leave_pending=True,
            )
            if attempt < MAX_ORDER_RETRIES:
                time.sleep(ORDER_RETRY_BACKOFF_S)
            continue
        elif status_up in _TERMINAL_ORDER_STATUSES:
            last_reason = status_up.lower() or "fak_no_fill"
            last_result = OrderResult(
                order_id=verify_order_id,
                limit_price=fill_limit,
                error=f"fak_no_fill: {last_reason}",
                success=False,
                verify_attempts=attempt,
            )
            return _finish(last_result)
        else:
            pending_order_id = verify_order_id
            last_reason = "timeout"

        last_result = OrderResult(
            order_id=verify_order_id,
            limit_price=fill_limit,
            fill_price=recorded_price,
            fill_size=recorded_shares,
            fill_ts=utc_now_sql(),
            error=f"verify_failed: {last_reason}",
            realized_edge=realized_edge,
            verify_attempts=attempt,
            fill_levels=recorded_levels,
        )
        if attempt < MAX_ORDER_RETRIES:
            time.sleep(ORDER_RETRY_BACKOFF_S)

    # --- Retries exhausted: hand off to terminal-state resolver ---
    return _finish(_resolve_terminal_state(
        signal,
        order_client,
        last_result=last_result,
        last_reason=last_reason,
        any_matched_no_tx=any_matched_no_tx,
        conn=conn,
        config=config,
    ))
