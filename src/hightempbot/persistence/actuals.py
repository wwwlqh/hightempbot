"""Shared SQL helpers for persisted actual temperature rows."""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=None)
def actual_source_clause(alias: str | None = None) -> tuple[str, tuple[str, ...]]:
    """SQL filter (and params) limiting actuals to supported sources. Cached."""
    from hightempbot.stations import SUPPORTED_LIVE_SOURCES

    sources = tuple(sorted(SUPPORTED_LIVE_SOURCES))
    if not sources:
        return "0", ()
    column = f"{alias}.source" if alias else "source"
    placeholders = ",".join("?" for _ in sources)
    return f"LOWER({column}) IN ({placeholders})", sources
