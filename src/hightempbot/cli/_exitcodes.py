"""Shared exit-code constants for hightempbot CLI entry points.

Keeps the per-script exit semantics consistent so operators and shell-driven
pipelines can reliably branch on the same numbers.

* OK         (0): success / proceed.
* USAGE      (1): argparse-style misuse, invalid arguments.
* NOGO       (2): operational refusal (preflight failed, safety gate tripped).
* UNEXPECTED (3): unhandled internal error / crash path.

Tests assert on stdout/stderr messages rather than exit codes, so adopting these
codes in existing CLIs is backward-compatible.
"""

from __future__ import annotations


OK: int = 0
USAGE: int = 1
NOGO: int = 2
UNEXPECTED: int = 3

__all__ = ["OK", "USAGE", "NOGO", "UNEXPECTED"]
