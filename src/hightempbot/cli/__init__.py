"""Operator CLIs.

Recurring operator tools (settle stranded PENDINGs, etc.) live here so they
ship with the regular src/ deploy and can be invoked as ``python -m
hightempbot.cli.<name>``. Six of them are also exposed as ``htb-*`` console
entry points (see ``[project.scripts]`` in ``pyproject.toml``).

Standalone operational helpers that are not part of the package — the
pre-deploy ledger duplicate check and the bot restart script — live under
``scripts/`` at the repo root and are run directly, not via ``-m``.
"""
