> Note (2026-09-11): the Oracle Cloud server (<server-ip>) is now the LuckyDraw host and the bot is no longer deployed there. The SSH key moved to ~/.ssh/luckydraw-oracle.key (alias: ssh luckydraw-oracle). Server runbook: luckydraw/docs/runbooks/oracle-server.md. The deploy commands below are historical.

---
title: Oracle Cloud Server
type: entity
entity_type: infrastructure
created: 2026-05-03
updated: 2026-05-22
tags: [entity, infrastructure, deployment]
status: developing
---

# Oracle Cloud Server

Production host for [[HighTempBot Project|HighTempBot]].

## Connection

- Address: `opc@<server-ip>`
- SSH key: `./ssh-key-2026-03-20.key` in the `hightempbot/` project root (full path on this machine: `C:\Users\leowq\OneDrive\Desktop\hightempbot\ssh-key-2026-03-20.key`). Moved from `~/Downloads/` on 2026-05-04. Gitignored via `ssh-key-*.key`. Windows ACL restricted to owner-read so OpenSSH accepts it. Run `ssh`/`scp` from project cwd.
- Strict host key checking disabled in deploy commands (`-o StrictHostKeyChecking=no`).

## Service layout

- App root: `~/hightempbot/`
- Entry: `python3.11 -m hightempbot.main`
- Restart script: `~/hightempbot/restart_bot.sh` (kills + starts the bot, which itself launches the dashboard in the same process)
- Dashboard: http://<server-ip>:8080/
- Current live PID after latest dashboard deploy: `2410422` (commit `2b0c448`, 2026-05-22)

## Live Polymarket env

- `DRY_RUN=False`
- `POLY_SIGNATURE_TYPE=3`
- `POLY_FUNDER=0xFf4eCB28218af1874da6086d30D1f3125fa1c843`
- Dashboard live smoke after the latest 2026-05-22 deploy: `capital=$108.07`, `totalPnl=$8.07`, `realizedPnl=$8.07`, `capitalSource=ledger_realized`, `resolvedCount=5`, `walletBalance≈$48.01`; Data API open-position marks are present for wallet/operator detail but ignored by the main Capital/P&L cards.
- Legacy proxy wallet balance after migration: about `$6`
- Current live account history includes 5 resolved wins via Gamma/Data API redeemable paths plus open positions from later ticks. `/health` may show `degraded` when recent station market/CLOB scans fail; that is distinct from dashboard liveness.

Check with:
```
cd ~/hightempbot
.venv/bin/python -m hightempbot.cli.poly_wallet_diagnostics --probe-clob
```

## Deploy gotcha

Never combine `kill` and `start` into a single SSH compound command — run them as separate SSH calls. The dashboard is started by `hightempbot.main` in the same process as the scheduler, so `restart_bot.sh` restarts both together.

Deploy the full `src/hightempbot` tree, not individual refactor files. On Windows PowerShell, direct binary tar pipes can corrupt gzip/stdin. The known-good route is:

1. `git archive --format=tar.gz --prefix=hightempbot/ HEAD:src/hightempbot` locally.
2. `scp` the archive to `/tmp` on the server.
3. Extract into `~/hightempbot/src/.deploy_staging`, then atomically replace `~/hightempbot/src/hightempbot`.
4. Keep the previous tree under `~/hightempbot/src.bak/hightempbot.<timestamp>` and restart with `~/hightempbot/restart_bot.sh`.

## See also

[[HighTempBot Project]] · FastAPI HTMX Dashboard · [[Weekly Retention Policy]]
