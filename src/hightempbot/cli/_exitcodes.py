"""CLI exit codes: OK 0, USAGE 1, NOGO 2 (safety refusal), UNEXPECTED 3."""

from __future__ import annotations


OK: int = 0
USAGE: int = 1
NOGO: int = 2
UNEXPECTED: int = 3

__all__ = ["OK", "USAGE", "NOGO", "UNEXPECTED"]
