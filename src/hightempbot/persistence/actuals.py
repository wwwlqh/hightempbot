"""Shared SQL helpers for persisted actual temperature rows."""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=None)
def actual_source_clause(alias: str | None = None) -> tuple[str, tuple[str, ...]]:
    """Return a SQL source filter for live-supported actual rows.

    Cached because ``SUPPORTED_LIVE_SOURCES`` is a frozenset that never
    changes at runtime — the hot-path callers (per-tick betting, resolution)
    hit this 20+ times per tick.
    """
    from hightempbot.stations import SUPPORTED_LIVE_SOURCES

    sources = tuple(sorted(SUPPORTED_LIVE_SOURCES))
    if not sources:
        return "0", ()
    column = f"{alias}.source" if alias else "source"
    placeholders = ",".join("?" for _ in sources)
    return f"LOWER({column}) IN ({placeholders})", sources
