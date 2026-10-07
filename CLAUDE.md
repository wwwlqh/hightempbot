> Note (2026-09-11): the Oracle Cloud server (<server-ip>) is now the LuckyDraw host and the bot is no longer deployed there. The SSH key moved to ~/.ssh/luckydraw-oracle.key (alias: ssh luckydraw-oracle). Server runbook: luckydraw/docs/runbooks/oracle-server.md. The deploy commands below are historical.

# HighTempBot — Project Instructions

## Deployment

- Server: `opc@<server-ip>`
- Key: `./ssh-key-2026-03-20.key` (in project root, gitignored; run commands from project cwd)
- Bot: `python3.11 -m hightempbot.main` in `~/hightempbot/`
- Dashboard: https://<server-ip>:8080/ (nginx terminates TLS on 8080 → app on 127.0.0.1:18080; plain http on 8080 returns nginx 400)

## Deploy Commands

```bash
# Pre-deploy: verify no duplicate order_id rows (else init_db will hard-fail
# on the partial UNIQUE INDEX added 2026-05-13). Exit 0 = proceed.
ssh -i "./ssh-key-2026-03-20.key" -o StrictHostKeyChecking=no opc@<server-ip> \
  "python3.11 ~/hightempbot/scripts/check_ledger_order_id_duplicates.py ~/hightempbot/data/hightempbot.db"

# Deploy full src tree. Target is `~/hightempbot/src/hightempbot/` because
# the server uses src-layout (pip install -e .). Falls back to tar pipe
# if rsync isn't installed locally. Partial scp turns refactor-renames into
# silent runtime crashes — see memory `full_module_deploy`.

# Preferred (when rsync is available locally):
rsync -av --delete -e "ssh -i ./ssh-key-2026-03-20.key -o StrictHostKeyChecking=no" \
  src/hightempbot/ opc@<server-ip>:~/hightempbot/src/hightempbot/

# Fallback (tar pipe + atomic blue-green swap, no rsync needed):
tar -czf - --exclude='__pycache__' --exclude='*.pyc' -C src hightempbot | \
  ssh -i "./ssh-key-2026-03-20.key" -o StrictHostKeyChecking=no opc@<server-ip> '
set -e
TS=$(date -u +%Y%m%dT%H%M%SZ)
STAGE="$HOME/hightempbot/src/.deploy_staging"
rm -rf "$STAGE" && mkdir -p "$STAGE" && cd "$STAGE" && tar -xz
mkdir -p "$HOME/hightempbot/src.bak"
mv "$HOME/hightempbot/src/hightempbot" "$HOME/hightempbot/src.bak/hightempbot.$TS"
mv "$STAGE/hightempbot" "$HOME/hightempbot/src/hightempbot"
rm -rf "$STAGE"
echo "swapped; backup at ~/hightempbot/src.bak/hightempbot.$TS"
'

# Restart bot
ssh -i "./ssh-key-2026-03-20.key" -o StrictHostKeyChecking=no opc@<server-ip> "bash ~/hightempbot/restart_bot.sh"
```

See `AGENTS.md` for full agent workflow + trading invariants.

## Dashboard Bind-Host (ce-code-review P0 #2)

- `DASHBOARD_BIND_HOST` (default empty): explicit bind host for the dashboard.
  When empty, the legacy fallback applies — `0.0.0.0` if `DASHBOARD_PASS` is
  set, else `127.0.0.1`. To force loopback even when a password is set, write
  `DASHBOARD_BIND_HOST=127.0.0.1`. To force public bind without a password
  (not recommended), write `DASHBOARD_BIND_HOST=0.0.0.0`.
- `DASHBOARD_TLS_TERMINATED` (default `false`): set to `1`/`true` when a
  reverse proxy (nginx/Caddy) terminates TLS in front of the bot. When the
  bind host is non-loopback AND this flag is unset, startup emits a Telegram
  warning and a log line — behavior is **not refused** so existing production
  (currently bound to `0.0.0.0` on bare HTTP behind a firewall) keeps working.
