# Verified revenue Telegram alerts

The independent `sofly-revenue-alerts.timer` checks once per minute. It reads
`appstorerevenueevent` with SQLite `mode=ro` and never imports application models,
changes the app database, or participates in checkout. Only Apple-verified
`Production` rows qualify: zero-price free trials or positive-price payments
(including renewals). Revoked rows and non-trial zero-price rows do not qualify.
Amounts are gross customer payments in the original currency, not proceeds.
Messages contain no customer, transaction, flight, or acquisition identifiers.

## Activation

1. Deploy the committed worker and unit files without restarting API/fetcher.
2. Supply `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from the existing
   `sofly/monitor/telegram` AWS secret in root-owned mode-0600
   `/etc/sofly-revenue-alerts.env`. Never print secrets or put them in arguments.
3. Create `/var/lib/sofly-revenue-alerts` owned by ubuntu with mode 0700. Run once:

   ```sh
   sudo -u ubuntu /usr/bin/python3 scripts/telegram_revenue_alerts.py \
     --db /home/ubuntu/backend-flight-tracker/database.db \
     --state /var/lib/sofly-revenue-alerts/state.db --initialize
   ```

4. Install the two unit files to `/etc/systemd/system`, reload systemd, then enable
   and start the timer. Start the service once to verify the activation message.

The baseline excludes every existing transaction ID and later-arriving historical
purchases with effective dates before activation. Delayed new transactions after
activation remain eligible. Apple sometimes reports renewals before their effective
date; these are notified immediately and clearly labeled, not sent twice.
An incomplete new transaction can become eligible after metadata enrichment.

## Delivery and recovery

The separate state DB is required, persistent, permission-restricted, and protected
by a process lock. Preserve it in backups. Missing state fails closed; initialization
refuses to reset existing history. Never delete state to fix a delivery problem.

Acknowledged sends become `sent`. Explicit Telegram rejections are retried with
backoff. Timeouts, ambiguous responses, or crashes during sends become `unknown`
and are **not automatically resent**, because Telegram has no idempotency key.
This prevents automatic duplicates but can require a human to confirm delivery.
An unknown delivery keeps the unit failing visibly until reviewed. Check the actual
channel before any manual state repair; do not blindly reset or replay alerts.
`suppressed` means the transaction was revoked or unavailable before delivery.
There is no exactly-once guarantee or separate notification if this worker fails.

```sh
sudo journalctl -u sofly-revenue-alerts.service --since '1 hour ago' --no-pager
sudo -u ubuntu /usr/bin/python3 scripts/telegram_revenue_alerts.py \
  --db /home/ubuntu/backend-flight-tracker/database.db \
  --state /var/lib/sofly-revenue-alerts/state.db --status
systemctl list-timers sofly-revenue-alerts.timer
```

Logs contain only aggregate state counts or generic failures, never Telegram tokens,
API response bodies, or customer identifiers. Credential rotation requires securely
refreshing the environment file; the next oneshot invocation loads it.

Rollback: disable and stop `sofly-revenue-alerts.timer` and stop its service. Keep
the delivery DB and credentials intact for a safe resume. Existing outage alerts
are independent and unchanged. Refunds and auto-renew toggles are not sent by this
worker; it reports new trials and payments only.
