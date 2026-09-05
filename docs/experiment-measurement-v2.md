# Flight-detail paywall measurement revision 2

The original `paywall_flight_detail_2026_09` cohort started counting when a paywall
was presented. Control had an extra flight-preview screen before that point.
New installations enroll at the shared flight-selection transition. Never pool
these denominators or enroll an already-exposed revision-1 installation again.

## Assignment and enrollment

`POST /subscriptions/experiments/assignment` accepts the existing assignment
request plus `assignment_locked: true` once an installation was enrolled/exposed,
and optional `measurement_revision` (1 or 2). `variant` preserves a locked cohort;
`effective_variant` and `experiment_enabled` describe current delivery. Continue
refreshing this response after exposure so off/control modes remain operational.

`POST /subscriptions/experiments/enrollment` is authenticated as the Sofly user:

```json
{
  "experiment": {
    "experiment_id": "paywall_flight_detail_2026_09",
    "variant": "control_current_paywall",
    "eligible": true,
    "installation_id": "11111111-2222-4333-8444-555555555555",
    "exposure_id": "paywall_flight_detail_2026_09:11111111-2222-4333-8444-555555555555",
    "app_version": "3.7",
    "build_number": "117",
    "analytics_environment": "development",
    "measurement_revision": 2
  },
  "measurement_revision": 2,
  "enrolled_at_ms": 1788624000000,
  "effective_variant": "control_current_paywall",
  "assignment_source": "deterministic_split",
  "config_version": "1"
}
```

The last three fields are optional. Persist and retry the exact original request.
Enrollment is idempotent by experiment, installation, and measurement revision.
`/experiments/exposure` continues to represent actual paywall exposure; enrollment
does not assert that a screen appeared or a purchase happened.

## Protected reporting

`GET /subscriptions/experiments/{experiment_id}/summary` requires the existing
Lambda/admin bearer token. Supported filters are `measurement_revision=1|2`,
`app_version`, inclusive `since_ms`, exclusive `until_ms`, `product_id`,
`acquisition_source=apple_ads|unknown`, and `horizon_days=14|30`.

The flight-detail experiment defaults to revision 2; earlier activation experiments
default to revision 1. The lifecycle summary accepts the same revision boundary.
No historical missing denominator is reconstructed or guessed.

- `verified_transaction_conversion_rate` includes verified free trials.
- `purchase_conversion_rate` and `verified_purchase_installations` remain documented
  compatibility aliases and must never be described as paid conversion.
- `paid_conversion_rate` requires a verified positive-price, nontrial transaction,
  including server-observed renewals linked to the experiment's original transaction.
- Mature trial conversion uses verified trial expiry (or known trial duration) and
  a later positive-price transaction. Unknown maturity is explicit.
- D14/D30 revenue includes only installations that completed that horizon. The
  horizon is measured from enrollment; a normal day-7 charge is included in D14.
  Revenue is split by currency and reports gross customer payments and refunds;
  it is not developer proceeds after Apple's commission and taxes.
- Product filters select outcomes without removing nonpurchasers from the denominator.
- Acquisition is a verified Apple Ads attribution joined by Sofly user ID. Missing
  attribution is `unknown`, never an assertion of organic or non-Meta acquisition.

## Durable sequence diagnostics

`POST /subscriptions/experiments/events` takes `{ "events": [...] }`, at most 25
events per request, with authenticated user ownership. Each event contains:

- `event_id` (stable UUID), `event_name` (allowlisted), `occurred_at_ms`,
  `installation_id` (UUID), `app_version`, `build_number`;
- `analytics_environment` (`production`, `testflight`, `development`) and
  `build_configuration` (`release`, `debug`);
- optional `paywall_presentation_id`, `checkout_attempt_id`, `experiment`;
- `properties`, a strict typed allowlist. Query text, freeform error text, URLs,
  emails, receipts and tokens are not accepted fields.

Paywall views/default selections require presentation IDs. Checkout start and
`checkout_attempt_completed` require both presentation and attempt IDs; the latter
has exactly one outcome: `verified`, `cancelled`, `pending`, `unverified`, `error`.
An asynchronous verification after a pending outcome is a separate purchase fact,
not a second terminal outcome for that attempt.

Exact retries deduplicate by event ID. A conflicting event ID or second terminal
event for an attempt returns 409; validation errors return 422. Clients must isolate
invalid items instead of indefinitely blocking the rest of a durable queue.
Diagnostics retain at most 90 days and accept at most 10,000 events per
authenticated user in a rolling day. They are operational evidence, not a
replacement for permanent verified subscription facts.

`GET /subscriptions/experiments/events/report` requires the Lambda/admin bearer
token. Filter by `installation_id`, `analytics_environment` (production by default),
`since_ms`, `until_ms`; limit is 500 by default, maximum 2,000. The report identifies
counts per presentation/attempt and whether a referenced transaction was separately
verified on the backend. Client claims never populate verified financial metrics.

Debug events must declare development. TestFlight/sandbox validation must use its
separate environment. A successful local build or diagnostic replay proves code or
delivery behavior only. Release/TestFlight live AppsFlyer raw events, plus verified
StoreKit facts, are still needed before claiming full live tracking verification.

Deployment creates two new tables through existing SQLModel startup initialization;
it does not alter the old exposure/conversion tables or rewrite historical records.
