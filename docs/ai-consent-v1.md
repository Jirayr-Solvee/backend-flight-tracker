# Explicit Google Gemini consent, policy version 1

Search and forwarded-email processing are separate account-owned permissions.
Neither a legacy account, an app version, a forwarded message, a Sign in with
Apple action, nor a permission for the other purpose constitutes consent.
Missing records and mismatched policy versions deny processing.

## Wire contract

All endpoints require the ordinary account bearer token. Never put that token,
Apple identity tokens, search text or email content in diagnostic output.

`GET /users/me/ai-consent` returns:

```json
{
  "user_id": "11111111-1111-4111-8111-111111111111",
  "policy_version": 1,
  "revision": 0,
  "search_enabled": false,
  "forwarded_email_enabled": false,
  "forwarded_email_verified": false,
  "updated_at": null
}
```

`updated_at`, when set, is an integer Unix timestamp in **milliseconds**.
`PUT /users/me/ai-consent` updates exactly one purpose:

```json
{
  "user_id": "11111111-1111-4111-8111-111111111111",
  "policy_version": 1,
  "expected_revision": 0,
  "purpose": "search",
  "enabled": true,
  "request_id": "33333333-3333-4333-8333-333333333333"
}
```

The purpose is `search` or `forwarded_email`. Capture the owner, bearer token,
permission snapshot and local account generation together before asynchronous
work. The required body owner must equal the authenticated account. Responses
must still belong to that frozen owner before the client uses them.

The successful response is the full current snapshot. Revision checks serialize
competing writes. The same request UUID and identical command is an idempotent
replay, returning **current** state, not its historical enabled state. Reusing
the UUID with different command fields is a conflict. Clients must inspect
the returned permission; a 200 does not necessarily mean an old Allow remains
enabled after a subsequent revocation.

Errors use FastAPI's `{"detail": {...}}` envelope:

- 409 `ai_consent_account_mismatch`: frozen owner differs from authenticated owner.
- 409 `ai_consent_conflict`, plus `consent` containing the current snapshot:
  stale revision or conflicting reuse of the request UUID. Refresh and require
  explicit user intent again; never blindly resubmit an old Allow.
- 409 `ai_consent_email_verification_required`: email Allow requires independently
  proven sender identity. Revocation is allowed without that proof.

## Email identity verification

Historical `User.email` accepted client input. Neither it nor `User.verified`
proves email ownership. `UserAIEmailIdentity` is populated only from a verified
Apple identity token whose subject matches the current account's Apple subject,
with a nonempty email and `email_verified` equal to boolean `true` or string
`"true"`. Legacy addresses are never backfilled into this proof table.

Normal Apple sign-in can record proof without changing the established account
UUID. Existing signed-in accounts can reauthenticate without account adoption:

`POST /users/me/ai-consent/verify-email` accepts `user_id` and `apple_jwt` in the
body and returns the same permission snapshot. It never signs in a guest,
merges accounts or issues a replacement session. The account and Apple subject
are rechecked under the database writer lock after Apple verification completes.
The permission revision captured before verification is also rechecked under
that lock. An older completion cannot overwrite a newer proof or consent change;
it returns `ai_consent_conflict` with current state instead.

Additional verification errors:

- 409 `ai_consent_sign_in_required`: account is not already Apple-linked.
- 409 `ai_consent_account_mismatch`: Apple subject differs, including an account
  identity change while verification was pending.
- 409 `ai_consent_email_verification_required`: signed verified-email claim absent.
- 401 `ai_consent_identity_verification_failed`: token verification failed.

Proof alone never enables email processing. Changing the proven email revokes
the old email permission and advances the revision. Apple's private relay
address may not match the mailbox the user forwards from; this must fail closed,
not be worked around by trusting a client-supplied address.

## Provider boundary and legacy behavior

The sole Gemini SDK send checks a fresh database snapshot immediately before
every request, including every retry. Email sends additionally require the
original frozen sender to still uniquely match the proven account/Apple subject.
An account deletion, revocation, changed sender, stale policy, missing proof or
database error cannot authorize an SDK call. Revocation stops new requests; it
cannot undo an already authorized request that is in flight or data already sent.

Each explicit email Allow creates an opaque grant generation and grant timestamp.
Revocation clears both; enabling search does not alter the email generation.
Legacy enabled flags with no generation remain denied until a new explicit Allow.
The current generation must have existed strictly before SES received the message.
Enabling or re-enabling permission later never authorizes a queued older email.

## Trusted forwarded-email intake

The Lambda accepts one direct SES receipt event with an exact `Event` or
`RequestResponse` Lambda action, not an S3 notification or a MIME-supplied
authentication result. It checks the configured function ARN,
recipient, untruncated single sender header and SES authentication/scan verdicts.
DMARC, spam and virus verdicts must be PASS; at least SPF or DKIM must be PASS.
The authenticated single From mailbox must match SES common headers and the
stored MIME sender. This is domain-authenticated mail plus an independently
verified Apple mailbox binding, not a new client-supplied identity proof.

The message identifier is SES's generated `mail.messageId`, never a MIME
Message-ID. The configured bucket and `authenticated-v1/` prefix determine the
only allowed object key. The Lambda reads at most 20 MiB, binds the exact bytes
with SHA-256, ETag and an optional version identifier, and sends only the bounded
receipt proof to the authenticated backend endpoint. The receipt is valid for
six hours after SES's mail receipt timestamp; future timestamps are rejected.
SES's later action timestamp may differ but cannot precede receipt or be stale.

These checks depend on production infrastructure: the protected prefix must be
writable only by the scoped SES role, and non-SES Lambda invocation must be
explicitly denied. A broad administrative ability to replace code or IAM policy
is outside the application trust boundary. A public-looking SES JSON document,
a role-name-only allow statement, or a bearer token alone does not establish
trusted SMTP provenance. The deployment operator must verify actual final
policies and direct-SES delivery, not just the local parser.

The backend rejects legacy bucket/key-only payloads. It revalidates the proof,
conditionally reads the exact object with `IfMatch` and optional `VersionId`,
and verifies bytes and sender before parsing private body/PDF data. It then
claims a durable receipt under the database writer lock, freezing the unique
Apple owner, current preexisting grant generation and proof digest. Every
actual Gemini SDK attempt rechecks that receipt, identity, grant and age using
a fresh database snapshot. Provider lookup and account linking retain the same
context. Database failures deny processing; private payloads and exception
details are never attached to intake diagnostic logs.

`UserAIEmailReceipt` is a global at-most-once tombstone. Exact duplicate receipts
do not start another processing job; a reused identifier with changed proof is
rejected. Claims are never automatically reclaimed, including after a crash or
lost acknowledgement. This intentionally favors preventing unauthorized/replayed
external sends over guaranteed import delivery. One authorized job may make up
to three SDK attempts, each independently consent-checked; it is not a claim of
exactly-once provider execution. Recovery requires a newly forwarded message
after a current explicit Allow, not deletion/replay of the old tombstone.

The tombstone stores only receipt/proof/owner-binding hashes, receipt/claim/end
timestamps and a bounded result state. It stores no raw email, object key,
sender, account identifier, grant identifier or message identifier. It survives
account deletion to prevent old mail being adopted by another account; all
account-owned consent commands and Apple proof are deleted normally. Tombstones
currently have no automatic expiration. S3 email objects have separate storage
and retention controls; this migration does not change or assert their policy.

HTTP 202 and `lambda_email_intake_accepted` mean backend scheduling only. They do
not prove consent, Gemini egress, a saved flight, APNs delivery or device display.
Success logging is enabled on the Lambda module only, not on SDK/root loggers.
Intentional terminal Lambda returns use SES `STOP_RULE_SET`, including permanent
rejections; asynchronous invocations ignore return payloads. Synchronous work has
a 20-second soft budget further limited by Lambda remaining time minus a two-second
reserve. Chunk and stage checks cannot interrupt blocking I/O or guarantee a
controlled disposition before the runtime/SES timeout. Operational exceptions
remain bounded; they are not a documented SES retry or SMTP rejection request.
See [synchronous intake rollout](ses-synchronous-intake.md) for deployment limits.

Deterministic flight-number, airport, route, registration and preflight-recovery
paths do not require Gemini consent. AI-required search with no permission:

- Modern `POST /flights/search/term`: 403
  `{"detail":{"code":"ai_consent_required","purpose":"search","policy_version":1}}`.
- Legacy `GET /flights/search/term`: existing recovery envelope with the bounded
  reason/failure reason `ai_consent_required`, provider outcome `not_called`,
  and an edit/search-help action.
- Consent-store outage: 503 `ai_consent_unavailable`, not a provider zero result.

Consent failures do not retain a backend failed-query sample. New clients must
continue using POST, since old GET clients inherently put text in a URL.

## Deployment

Before restarting updated services, run both additive migrations against the
explicit existing production database (after the normal database backup):

```sh
venv/bin/python scripts/migrate_device_localized_push_version.py /home/ubuntu/backend-flight-tracker/database.db
venv/bin/python scripts/migrate_ai_consent.py /home/ubuntu/backend-flight-tracker/database.db
```

The AI migration creates `useraiconsent`, `aiconsentreceipt`,
`useraiemailidentity`, and `useraiemailreceipt`, adding nullable email-grant ID
and timestamp columns when upgrading an earlier consent schema. It does not
backfill existing account identity or grant any permission. Both migrations
validate existing schemas, defaults and constraints, and roll back transactionally
on a mismatch. They are idempotent and require an explicit existing SQLite file.
Fresh application databases get compatible registered SQLModel tables. Account
deletion clears the three account-owned tables but retains the global hashed
email-receipt tombstones.

Set `FORWARDED_EMAIL_BUCKET` explicitly before startup; an absent bucket fails
closed. The default key prefix is `authenticated-v1/` and intended recipient is
`track@sofly.to`. The Lambda ZIP contains exactly the handler and a verbatim copy
of `core/email_ingress_contract.py` at ZIP root as `email_ingress_contract.py`.
Supply its existing credential securely via `LAMBDA_FUNCTION_AUTH_TOKEN`, plus
`BACKEND_URL=https://api.sofly.to`, the three forwarded-email storage/recipient
settings and `FORWARDED_EMAIL_LAMBDA_ARN`. Never write secrets into source or
retained deployment payloads. Keep the old S3-only invocation path disabled.
The two-mode handler can be installed before changing SES's action to
`RequestResponse`. That separate mode switch and proposed 25-second Lambda
runtime backstop require operator approval and exact configuration readback;
local tests do not establish either has been deployed.

Follow the repository's sequential fetcher/API readiness gates and authenticated
functional checks. A safe consent smoke uses a disposable guest and synthetic
text: default denial, owner mismatch, search grant, revoke, old receipt replay,
AI-required POST 403, deterministic search result, email grant refusal without
verified identity, and final guest deletion. Do not trigger Google processing
merely to test a grant; actual sending is covered with stubbed SDK tests.
Do not roll back to consent-less backend code or the legacy S3-only Lambda.
Keep intake fail-closed and repair forward if readiness or proof fails; do not
restore an old database over later consent or verified transaction facts.

## Isolated regression suite

Run from a disposable directory so legacy import-time model initialization does
not touch the local application database. Set `PYTHONPATH` to the repository and
invoke its virtual-environment Python:

```sh
python -m unittest tests.test_email_ingress tests.test_ai_consent tests.test_migration_schema_validation tests.test_gemini_credential_logging tests.test_flight_notifications tests.test_legal_pages tests.test_credential_logging tests.test_search_recovery tests.test_search_failure_reporting tests.test_release38_contracts
```

The new consent suite uses per-test temporary SQLite files and stubbed Apple,
Gemini and S3. It exercises authenticated wire JSON, frozen-owner rejection,
strict fields, concurrent CAS/replay, revocation between actual SDK attempts,
account identity changes during verification, deleted/recreated accounts,
untrusted client email, body/PDF boundaries, ambiguous senders, sender changes,
legacy deterministic routes, modern typed 403 and migration retry/default denial.
The receipt suite additionally covers forged/legacy events, MIME sender ambiguity,
object substitution, delayed action timestamps, pre-grant receipts, revoke/regrant,
concurrent claim winners, crash/replay tombstones, owner rebinding, SDK-retry
denial, redirect refusal, bounded logging and Lambda-to-authenticated-HTTP-to-SDK
boundary-to-persisted-link behavior with synthetic storage and provider stubs.
These local fixtures do not prove AWS IAM/SES configuration or real SMTP delivery.
