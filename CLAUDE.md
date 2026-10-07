# HighTempBot — Claude instructions

The bot is retired (stopped September 2026). The old Oracle server now hosts
LuckyDraw; its key is `~/.ssh/luckydraw-oracle.key` (`ssh luckydraw-oracle`),
runbook in `luckydraw/docs/runbooks/oracle-server.md`. Do not deploy there.

Read `AGENTS.md` for the code map and trading invariants.

## If the bot is ever redeployed

- Run as `python3.11 -m hightempbot.main` from `~/hightempbot/` (src layout,
  `pip install -e .`). `scripts/restart_bot.sh` restarts bot + dashboard.
- Deploy the whole `src/hightempbot/` tree (rsync `--delete` or tar), never
  single files — partial copies break renamed imports at runtime.
- Before deploying, run `scripts/check_ledger_order_id_duplicates.py <db>`;
  `init_db` refuses to start if duplicate `order_id` rows exist.
- Dashboard bind: `DASHBOARD_BIND_HOST` (empty = `0.0.0.0` if
  `DASHBOARD_PASS` is set, else `127.0.0.1`). Set
  `DASHBOARD_TLS_TERMINATED=1` when a reverse proxy terminates TLS.
