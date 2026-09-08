# Failed-search sample retention

## Contract and scope

The active SQLite `searchfailuresample` table contains the encrypted, redacted
query examples. Its original server capture timestamp never changes. Logical
expiry is **six days, 23 hours, 50 minutes** after capture: ten minutes before the
public seven-day upper bound. A lower existing expiry is preserved. Historically
retry-extended expiries are capped by the same original-capture rule.

Only a fresh request handled by the backend flight-search handler may create a
sample. An app report's `source` is not trusted as capture authority. App reports
with no ID, a missing/deleted/expired ID, a foreign ID, or conflicting query or
correlation identity are successful no-ops; they do not encrypt or retain another
query/digest. This intentionally gives up raw examples for client-only failures
without an issued backend ID. Existing clients remain compatible: their diagnostic
submission is best-effort and checks HTTP success without decoding the response.
There is no permanent query/replay receipt or additional retention category.

For a live owned ID, app outcome/count enrichment may continue without changing
the ciphertext, digest, capture timestamp, or expiry. Backend app/build/environment
and provider timing/type/outcome remain frozen even when originally unknown.
Missing journey/attempt fields can bind once for correlation; that binding is not
evidence that previously unknown capture metadata was historically known.

Expired samples are excluded from the protected report without relying on a
cleanup transaction. Decryption requires capture/expiry context and checks the
deadline before and after decryption; report construction checks again. Response
acknowledgements freeze scalar values before commit so a simultaneous sweep cannot
turn an accepted report into a stale-ORM-object error.

The independent cleanup command deletes **only expired search samples** and
clamps live legacy sample expiries. It also maintains one aggregate operational
status row with counts/times only. It does not delete users, flights, transactions,
financial records, forwarded emails, S3 objects, database files, or backup files.

Scope is the explicitly selected active database and its active SQLite journal.
Known historical copies must be inventoried and, where necessary, separately
authorized and remediated before claiming retention rollout complete. This runner
must not be reused as an expired-rows-only backup sanitizer: it also clamps live
expiry metadata and writes aggregate cleanup status. No assertion is made about
all replicas, uninspected backups, filesystem snapshots, or forensic disk erasure.

## Bounded independent cleanup

`scripts/cleanup_search_failures.py` uses only Python's standard library and a
directly loaded, credential-free policy module. It deliberately does **not** import
`core`, load `.env`, initialize the app, or contact a provider. The selected
database must already exist and have the expected diagnostic table shape.
Default/dry-run and `--status` open it read-only; deletion requires `--apply`.

The installed job uses batches of at most 500 deletes and 500 live-expiry clamps,
at most 40 batches, and a 20-second deadline. SQLite busy waits and statement
progress are bounded. A remaining backlog, database failure, or blocked WAL
checkpoint produces a nonzero exit; failure does not advance the last successful
sweep. Secure-delete overwrites deleted active database cells, and WAL mode also
requires a successful truncate checkpoint. No journal-mode change or whole-database
vacuum is performed.

The timer runs every UTC minute independently of HTTP traffic. An API service
drop-in performs the same bounded sweep **before startup/restart serves traffic**.
The ten-minute headroom accommodates normal scheduling/lock delay; it is not an
outage-proof guarantee of physical deletion. Investigate failures or stale status,
and do not claim compliant operations from timestamps alone.

## Installation and rollout gates

Run these only as part of an authorized deployment after any separately scoped
historical-copy issue has been resolved. Preserve existing unrelated unit settings.

```bash
cd /home/ubuntu/backend-flight-tracker
sudo install -d /etc/systemd/system/flight-tracker.service.d
sudo install -m 0644 ops/systemd/sofly-search-failure-cleanup.service /etc/systemd/system/sofly-search-failure-cleanup.service
sudo install -m 0644 ops/systemd/sofly-search-failure-cleanup.timer /etc/systemd/system/sofly-search-failure-cleanup.timer
sudo install -m 0644 ops/systemd/flight-tracker.service.d/search-failure-retention.conf /etc/systemd/system/flight-tracker.service.d/search-failure-retention.conf
sudo systemd-analyze verify /etc/systemd/system/sofly-search-failure-cleanup.service /etc/systemd/system/sofly-search-failure-cleanup.timer
sudo systemctl daemon-reload
sudo systemctl start sofly-search-failure-cleanup.service
sudo systemctl show sofly-search-failure-cleanup.service -p Result -p ExecMainStatus
sudo systemctl enable --now sofly-search-failure-cleanup.timer
sudo systemctl is-active sofly-search-failure-cleanup.timer
sudo systemctl list-timers sofly-search-failure-cleanup.timer --all --no-pager
```

Require `Result=success`, `ExecMainStatus=0`, and an active timer with a next trigger.
A successful oneshot normally becomes inactive after exit; `is-active` on the
oneshot is not its success gate. Do not bypass a failed initial or pre-start sweep.
Drain a bounded backlog with further explicitly observed runs, or investigate
locking/checkpoint errors, before continuing.

Read only aggregate status (no credentials or sample contents):

```bash
/usr/bin/env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 /home/ubuntu/backend-flight-tracker/venv/bin/python3 /home/ubuntu/backend-flight-tracker/scripts/cleanup_search_failures.py --database /home/ubuntu/backend-flight-tracker/database.db --status
```

Require `last_sweep_outcome=success`, `expired_remaining=0`, and
`last_success_stale=false`. The protected failure report also exposes an aggregate
`cleanup` object. A `--status`/dry-run exit of zero means inspection succeeded,
not that retention is healthy; a recent success can coexist with a later failed
sweep. Require the latest stored/systemd outcome as well as the counters above.
Unknown or future last-success timestamps are stale; a success
more than two minutes old is stale. Observe a later **automatic** timer-triggered
success with an advancing timestamp while no diagnostic HTTP traffic is required.
The job's own aggregate counters are the deletion evidence; never print queries,
ciphertext, digests, IDs, credentials, or driver exception text.

After the startup gate is installed, use the existing sequential fetcher/API
restart and HTTP readiness procedure in `AGENTS.md`. Require the API's pre-start
sweep and both HTTP readiness gates, then perform the scoped authenticated smoke.
Policy/publication must not precede the successful retention deployment gates.

## Local proof (no production data)

Run tests from an isolated working directory with an empty inherited environment;
`core/__init__.py` otherwise creates/opens `database.db` relative to the cwd.

```bash
retention_test_dir=$(mktemp -d /tmp/sofly-retention-tests.XXXXXX)
cd "$retention_test_dir"
/usr/bin/env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 AWS_EC2_METADATA_DISABLED=true PYTHONPATH=/Users/jirayrmelikyan/Workspace/sofly/backend-flight-tracker /Users/jirayrmelikyan/Workspace/sofly/backend-flight-tracker/.venv/bin/python -m unittest discover -s /Users/jirayrmelikyan/Workspace/sofly/backend-flight-tracker/tests -q
```

`tests/test_search_failure_retention.py` covers near/exact/late retries, deleted-ID
resurrection, forged backend source, no-ID legacy success, foreign and conflicting
IDs, immutable known/unknown capture metadata, concurrent independent sessions,
expiry while waiting for a writer, commit-to-cleanup interleaving, expiry between
selection/decryption and during decryption, idle/bounded cleanup, failure status,
WAL-reader interference, read-only audits, clean-environment/wrong-cwd operation,
and preservation of unrelated tables and backup files.

Local tests prove the code contracts. OS unit validation, installed timer/readback,
initial and scheduled deletion, startup gating, and independently authorized
historical-copy remediation remain deployment proofs, not local-test conclusions.
