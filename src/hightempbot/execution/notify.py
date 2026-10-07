"""Push notifications via Telegram.

Sends alerts for trading events, enrollment lifecycle, and system health.
Rate-limited per event_type:station_id to avoid spam.

Lifecycle events (discovered, enrolled, promoted) are exempt from rate limiting
since they fire at most once per station.

Critical-operational events (order_cancel_fail, order_retry_fail,
cancel_failed, startup_degraded) bypass the rate limit and ALSO mirror to
``logs/CRITICAL.log`` so an outage of Telegram cannot silently swallow the
only operator-visible signal (ce-code-review P1 #15). A freshly
``verification_downgraded`` order is matched with delayed chain proof, so it
stays in logs/pipeline health first; wallet reconciliation sends a Telegram
alert only if the tx hash is still missing after the delay window.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import threading
import time
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from hightempbot.runtime_config import Config

logger = logging.getLogger(__name__)

# Rate limit: per event_type:station_id, 15 minutes.
# `_last_sent` is touched by multiple scheduler threads; the lock guarantees
# the read-then-write check is atomic so two threads cannot simultaneously
# decide they are clear to send.
_last_sent: dict[str, float] = {}
_last_sent_lock = threading.Lock()
_RATE_LIMIT_SECONDS = 900  # 15 minutes

# Lifecycle events exempt from rate limiting (fire once per station)
_LIFECYCLE_EVENTS = {"station_discovered", "station_enrolled", "station_promoted", "station_paused"}

# Critical operational events that bypass rate limiting and ALSO get mirrored
# to logs/CRITICAL.log so a Telegram outage cannot drop them silently. These
# are alerts the operator MUST see (real-money safety, startup degradation,
# or persistent CLOB cancel failures). ce-code-review P1 #15.
_CRITICAL_EVENTS = {
    "order_cancel_fail",
    "order_retry_fail",
    "cancel_failed",
    "startup_degraded",
}

# Dedicated logger that writes to logs/CRITICAL.log. Lazy-initialized so
# importing this module never side-effects the filesystem (test fixtures
# import without expecting a log file).
_critical_logger: logging.Logger | None = None
_critical_logger_lock = threading.Lock()


def _get_critical_logger() -> logging.Logger:
    """Return the singleton CRITICAL.log handler-backed logger."""
    global _critical_logger
    if _critical_logger is not None:
        return _critical_logger
    with _critical_logger_lock:
        if _critical_logger is not None:
            return _critical_logger
        lg = logging.getLogger("hightempbot.critical")
        lg.setLevel(logging.ERROR)
        # Don't bubble to root -- the regular logger already captures these.
        lg.propagate = False
        if not lg.handlers:
            try:
                logs_dir = Path("logs")
                logs_dir.mkdir(parents=True, exist_ok=True)
                handler = logging.handlers.RotatingFileHandler(
                    logs_dir / "CRITICAL.log",
                    maxBytes=5_000_000,
                    backupCount=3,
                    encoding="utf-8",
                )
                handler.setFormatter(
                    logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
                )
                lg.addHandler(handler)
            except Exception:
                # Filesystem unavailable (read-only mount, permission error).
                # fall back to a stderr StreamHandler so
                # CRITICAL events still surface in process logs / journald.
                # Previously a missing CRITICAL.log silently dropped them.
                logger.warning("CRITICAL.log handler init failed; using stderr", exc_info=True)
                try:
                    import sys as _sys

                    stream = logging.StreamHandler(_sys.stderr)
                    stream.setFormatter(
                        logging.Formatter(
                            "%(asctime)s CRITICAL %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S",
                        )
                    )
                    lg.addHandler(stream)
                except Exception:
                    # Last resort: leave logger without handlers; messages
                    # become no-ops rather than crash the trading thread.
                    pass
        _critical_logger = lg
        return lg

_TELEGRAM_TOKEN_RE = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+/")
_TELEGRAM_TEXT_LIMIT = 3900
_MOJIBAKE_MARKERS = ("\u00c2", "\u00c3", "\u00e2", "\u00ce")


def format_alert_text(value: object | None) -> str:
    """Normalize operator-alert text for UTF-8-hostile relays.

    Telegram itself accepts UTF-8, but the operator may read messages through
    chat bridges that display common cp1252/latin-1 mojibake. Keep push-alert
    payloads ASCII where practical; dashboard/UI text can stay pretty.
    """
    if value is None:
        return ""

    text = str(value)
    for codec in ("cp1252", "latin-1"):
        if not any(ch in text for ch in _MOJIBAKE_MARKERS):
            break
        try:
            repaired = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired:
            text = repaired

    replacements = {
        "\u2265": ">=",
        "\u2264": "<=",
        "\u00b0": "",
        "\u00c2\u00b0": "",
        "\u00c2": "",
        "\u00e2\u2030\u00a5": ">=",
        "\u00e2\u2030\u00a4": "<=",
        "\u00e2\u20ac\u201d": "-",
        "\u00e2\u20ac\u201c": "-",
        "\u2014": "-",
        "\u2013": "-",
        "\u00e2\u2020\u2019": "->",
        "\u2192": "->",
        "\u00ce\u00b8": "theta",
        "\u03b8": "theta",
        "\u00c3\u2014": "x",
        "\u00d7": "x",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


def _redact_telegram(text: str) -> str:
    """Strip Telegram bot tokens from any URL embedded in ``text``.

    Used before logging exception messages so a leaked token doesn't end up
    in the log file.
    """
    if not text:
        return text
    return _TELEGRAM_TOKEN_RE.sub("/bot<REDACTED>/", text)


def _telegram_text(title: str, message: str) -> str:
    text = f"\U0001f6a8 {escape(format_alert_text(title))}\n{escape(format_alert_text(message))}"
    if len(text) <= _TELEGRAM_TEXT_LIMIT:
        return text
    return text[: _TELEGRAM_TEXT_LIMIT - 16].rstrip() + "\n...[truncated]"


def send_alert(
    title: str,
    message: str,
    config: Config | None = None,
    stage: str = "unknown",
    station_id: str = "",
) -> bool:
    """Send a push notification. Returns True if sent, False if skipped/failed.

    Args:
        title: Notification title
        message: Notification body
        config: App config with Telegram credentials
        stage: Event type for rate limiting (e.g., "drawdown", "station_discovered")
        station_id: Station ICAO for per-station rate limiting
    """
    if config is None:
        return False

    title = format_alert_text(title)
    message = format_alert_text(message)

    # Critical events ALWAYS mirror to CRITICAL.log, regardless of Telegram
    # success and regardless of rate limit. Operator safety net.
    if stage in _CRITICAL_EVENTS:
        try:
            _get_critical_logger().error(
                "[%s] %s: %s", stage, title, message.replace("\n", " | ")
            )
        except Exception:
            logger.warning("Failed to write CRITICAL.log entry", exc_info=True)

    # Rate limit key: event_type:station_id
    rate_key = f"{stage}:{station_id}" if station_id else stage

    # Lifecycle AND critical events skip rate limiting. For rate-limited
    # events, reserve the slot before sending so concurrent scheduler threads
    # cannot both pass the read/check and emit duplicate alerts.
    reserved_at: float | None = None
    if stage not in _LIFECYCLE_EVENTS and stage not in _CRITICAL_EVENTS:
        now = time.time()
        with _last_sent_lock:
            last = _last_sent.get(rate_key, 0)
            if now - last < _RATE_LIMIT_SECONDS:
                logger.debug("Notification rate-limited for %s", rate_key)
                return False
            _last_sent[rate_key] = now
            reserved_at = now

    sent = False

    # Try Telegram
    tg_token = config.notify_telegram_token.get_secret_value() if hasattr(config.notify_telegram_token, 'get_secret_value') else config.notify_telegram_token
    if tg_token and config.notify_telegram_chat_id:
        sent = _send_telegram(tg_token, config.notify_telegram_chat_id, title, message)

    if sent and reserved_at is not None:
        with _last_sent_lock:
            _last_sent[rate_key] = time.time()
    elif not sent and reserved_at is not None:
        with _last_sent_lock:
            if _last_sent.get(rate_key) == reserved_at:
                _last_sent.pop(rate_key, None)

    return sent


def _send_telegram(token: str, chat_id: str, title: str, message: str) -> bool:
    """Send via Telegram Bot API.

    The bot token appears as a URL path segment; do NOT log the URL or the
    raw exception (the requests traceback embeds the URL). All log output
    here goes through ``_redact_telegram`` to strip the token.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": _telegram_text(title, message),
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.exceptions.RequestException as exc:
        # Log the redacted exception message; never pass exc_info=True here —
        # the traceback would re-introduce the URL that we just stripped.
        logger.warning("Telegram notification failed: %s", _redact_telegram(str(exc)))
        return False
    except Exception as exc:
        logger.warning(
            "Telegram notification failed (%s): %s",
            type(exc).__name__,
            _redact_telegram(str(exc)),
        )
        return False
