# Analytics diagnostics schema 20

The authenticated diagnostics route accepts the iOS typed-event projection,
including app lifecycle, screens, restore, Copilot, notifications and the new
bounded interaction events. There are 84 accepted event names. This is an
allowlist contract, not a claim that every interaction emits or every device
successfully delivers an event.

New schema-20 names require these fields:

| Event | Required properties | Meaning |
| --- | --- | --- |
| `voice_search_action` | `action`, `source`, `has_transcript` | Bounded voice interaction; no transcript |
| `permission_result` | `permission`, `status`, `source` | Reported permission result |
| `setting_changed` | `setting`, `value`, `source` | Finite setting/value pair |
| `search_suggestion_selected` | `suggestion_kind`, `source` | Example-chip selection, never its text |
| `account_action` | `action`, `outcome`, `source` | Bounded account operation |
| `flight_import_action` | `action`, `outcome`, `source` | Forwarding-address share sheet, not an imported flight |
| `flight_deletion_outcome` | `flight_id`, `stage`, `outcome` | Local save or backend deletion outcome |

`flight_deleted` keeps its historical request/intent meaning. New clients mark
that intent with `stage=requested`. `flight_deletion_outcome` allows
`local_save` with `succeeded` or `failed`, and `backend_delete` with `succeeded`,
`failed` or `skipped`. A skipped backend operation is not deletion success.

The iOS projection intentionally excludes successful sign-out and account
deletion from authenticated backend diagnostics after identity loss. It must not
rebind those queued events to the next guest or resurrect a deleted account.
These exceptions do not remove `account_action` from the event-name allowlist.
The account-deletion HTTP response is independent evidence of that operation.

## Preserved boundaries

- Schema-20 onboarding capture without experiment/journey context is operational
  telemetry only. It creates no cohort or legacy protocol reservation and does
  not block a subsequent assignment. Pre-20 unscoped onboarding and explicit
  legacy experiment facts retain their historical migration role.
- Capture-time identity, environment and event IDs remain immutable. Exact
  retries deduplicate; changed facts or owners conflict. Debug capture remains
  in `development`, separate from `testflight` and `production`.
- The route is not a source of verified Apple purchases, trials or revenue.
  Share-sheet completion is not proof that an email arrived or a flight linked.
- Extra properties are rejected. Raw queries, transcripts, routes, callsigns,
  notification payloads, email addresses and arbitrary human labels are not part
  of the backend projection. Token syntax is not a PII detector: emitters must
  still supply fixed machine codes rather than user strings.
- Retention removes old diagnostic context rows before their parent events,
  while retaining permanent enrollment and protocol identity records.

## Local wire verification

`scripts/verify_analytics_wire.py` consumes the exact JSON emitted by the Swift
enum/codec harness. It checks the strict DTO and the complete client/server name
sets, sends the original event-object bytes through authenticated in-memory HTTP
routes, retries them unchanged, rejects arbitrary query properties, and checks
that no verified transaction/revenue rows were created.

Run it with an empty environment from a fresh temporary working directory; the
repository's existing model import otherwise creates a cwd-local SQLite schema.
Supply the exact repository `PYTHONPATH` and the two synthetic fixture paths:

```sh
verification_dir=$(mktemp -d)
cd "$verification_dir"
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  PYTHONPATH=/absolute/backend-flight-tracker PYTHONDONTWRITEBYTECODE=1 \
  /absolute/backend-flight-tracker/.venv/bin/python \
  /absolute/backend-flight-tracker/scripts/verify_analytics_wire.py \
  /absolute/synthetic/events.json /absolute/synthetic/coverage.json
```

The harness intentionally isolates legacy, journey and cohortless synthetic
contexts in separate databases because the source fixture reuses an installation
across them. It does not weaken or bypass the real migration boundary. A server
assignment example must match an actual generated test-server proposal.

This verifies the synthetic codec/ingestion contract. It does not establish
runtime presentation/tap timing, process-restart delivery, AppsFlyer SDK/raw-data
delivery, StoreKit verification, production deployment, APNs acceptance or device
rendering. Those require separately authorized integration and release checks.
