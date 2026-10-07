"""Telegram alerts, rate-limited per (event type, station).

Lifecycle events skip the rate limit. Critical events skip it too and are
also written to ``logs/CRITICAL.log`` in case Telegram is down.
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

_last_sent: dict[str, float] = {}
_last_sent_lock = threading.Lock()
_RATE_LIMIT_SECONDS = 900

_LIFECYCLE_EVENTS = {"station_discovered", "station_enrolled", "station_promoted", "station_paused"}

_CRITICAL_EVENTS = {
    "order_cancel_fail",
    "order_retry_fail",
    "cancel_failed",
    "startup_degraded",
}

# logs/CRITICAL.log logger, created on first use.
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
                # Can't write the file: log to stderr instead.
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
                    pass
        _critical_logger = lg
        return lg

_TELEGRAM_TOKEN_RE = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+/")
_TELEGRAM_TEXT_LIMIT = 3900
_MOJIBAKE_MARKERS = ("\u00c2", "\u00c3", "\u00e2", "\u00ce")


def format_alert_text(value: object | None) -> str:
    """Replace common non-ASCII symbols so alerts survive chat bridges."""
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
    """Remove Telegram bot tokens from ``text`` before logging it."""
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
    """Send an alert; ``stage`` and ``station_id`` key the rate limit.
    Returns True if sent."""
    if config is None:
        return False

    title = format_alert_text(title)
    message = format_alert_text(message)

    if stage in _CRITICAL_EVENTS:
        try:
            _get_critical_logger().error(
                "[%s] %s: %s", stage, title, message.replace("\n", " | ")
            )
        except Exception:
            logger.warning("Failed to write CRITICAL.log entry", exc_info=True)

    rate_key = f"{stage}:{station_id}" if station_id else stage

    # Reserve the rate-limit slot before sending so two threads can't both send.
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
    """Send via the Telegram Bot API. The token is in the URL, so only redacted
    text is ever logged."""
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
        # No exc_info: the traceback contains the token.
        logger.warning("Telegram notification failed: %s", _redact_telegram(str(exc)))
        return False
    except Exception as exc:
        logger.warning(
            "Telegram notification failed (%s): %s",
            type(exc).__name__,
            _redact_telegram(str(exc)),
        )
        return False
