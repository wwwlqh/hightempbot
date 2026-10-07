#!/bin/bash
# Versioned copy of the server's restart script (lives at ~/hightempbot/restart_bot.sh
# on opc@<server-ip>). Kept in-repo so the documented restart procedure survives
# server loss. If you change this file, copy it to the server as well — the server
# copy is the one that runs.
cd ~/hightempbot
pkill -f 'python3.11 -m hightempbot.main' 2>/dev/null
sleep 2
# Belt-and-braces: kill anything still holding the dashboard app port (orphan dashboard)
APP_PORT=$(awk -F= '$1=="DASHBOARD_PORT"{print $2}' .env | tail -n1 | sed 's/[^0-9].*$//')
APP_PORT=${APP_PORT:-8080}
fuser -k "${APP_PORT}/tcp" 2>/dev/null
sleep 1
source .venv/bin/activate
set -a
source .env
set +a
nohup python3.11 -m hightempbot.main > logs/bot_stdout.log 2>&1 &
echo "Bot started with PID $!"
