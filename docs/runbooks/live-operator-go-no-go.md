# Live GO/NO-GO

Read-only checks before going live or before a pUSD return transfer.

1. Check local and server `.env` agree on live-critical keys:

   ```bash
   python -m hightempbot.cli.env_parity --ssh-target opc@<host> --ssh-key <key>
   ```

2. Run the combined check:

   ```bash
   python -m hightempbot.cli.live_go_no_go --ssh-target opc@<host> --ssh-key <key> \
     --require-db-backup --db-backup data/hightempbot.db.bak
   ```

3. Use the dashboard Operator page. Stop pauses betting and TP/SL while the
   dashboard stays up; Start never overrides `DRY_RUN=True`. Transfer needs a
   fresh readiness report and wallet snapshot, no PENDING rows, open orders or
   unresolved positions, Transfer Lock, and exact confirmation text.

Balances come from the `POLY_FUNDER` wallet, not the ledger.

**Rollback**: keep a DB backup, the previous `src/hightempbot`, `.env` and
logs; restore them and run `scripts/restart_bot.sh`.
