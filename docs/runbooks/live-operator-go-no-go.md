# Live Operator GO/NO-GO

Use this when preparing the bot for live trading or before a pUSD return
transfer. It is read-only unless you explicitly press dashboard controls or run
a transfer submit.

## Confidence Ladder

- `plan-ready`: plan exists, but code evidence is missing.
- `code-ready`: schema/code/tests are present locally.
- `server-ready`: server env/code/DB backup checks pass.
- `live-capable`: env parity, live readiness, and fresh wallet snapshot pass.
- `live-confirmed`: a real live fill or close is reconciled back to
  `POLY_FUNDER` and visible on the dashboard.

Do not call the bot 100% live-confirmed until the last state is reached.

## Required Sequence

1. Align local and server `.env` for live-critical keys.

```bash
python -m hightempbot.cli.env_parity --ssh-target opc@<server-ip> --ssh-key ./ssh-key-2026-03-20.key
```

2. Run the combined GO/NO-GO check.

```bash
python -m hightempbot.cli.live_go_no_go --ssh-target opc@<server-ip> --ssh-key ./ssh-key-2026-03-20.key --require-db-backup --db-backup data/hightempbot.db.bak
```

3. Open the dashboard Operator page.

- Stop Processing pauses automated betting and TP/SL processing while the
  dashboard stays online.
- Start Processing only resumes an already-live process. It never overrides
  `DRY_RUN=True`.
- Transfer requires a fresh readiness report, fresh wallet snapshot, no live
  PENDING rows, no open CLOB orders, no unresolved wallet positions, Transfer
  Lock, and exact confirmation text.

## Wallet Source of Truth

The dashboard follows `POLY_FUNDER` first. Local ledger rows explain strategy,
station, and decision context, but live balances/trades/positions must reconcile
against the configured funder/deposit wallet.

Polygonscan is raw on-chain audit history. It is not a wallet-login or withdraw
UI. Withdraw through Polymarket or through the dashboard return-transfer flow
once it is fresh and eligible.

## Cleanup

The 2026-05-21 cleanup manifest was applied in commit `cc6a25d` and now has no
pending delete items. For future cleanup, create a new manifest and run it in
dry-run mode first:

```bash
python -m hightempbot.cli.cleanup_manifest <manifest> --json
```

Never delete `.env`, SSH keys, SQLite databases, WAL/SHM files, live logs, or
unknown server files. Flip `proof.unused` to true only after code and docs no
longer reference the file.

## Rollback

Before server restart, keep:

- current DB backup
- previous `src/hightempbot` backup
- current `.env`
- current logs

Rollback is restoring the previous source tree and `.env`, then restarting:

```bash
ssh -i "./ssh-key-2026-03-20.key" -o StrictHostKeyChecking=no opc@<server-ip> "bash ~/hightempbot/restart_bot.sh"
```
