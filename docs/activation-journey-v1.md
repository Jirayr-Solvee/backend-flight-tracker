# Activation journey v1 — implemented, not launched

Protocol: `activation_journey_2026_09`, revision `1`. The original prepared journeys are
`search_first_standard` and `goals_flight_detail`. September 11 adds the separately
named `search_first_flight_detail` candidate; it does not rename either original
arm or migrate existing assignments. This is not a new revision of
the historical selected-flight paywall experiment. The public build and UTC
launch boundary remain unset. All tests below use synthetic data.

Both production and nonproduction enrollment settings default to **false**.
There has been no deployment, production configuration change, production
enrollment, existing-monitor change, or App Store upload as part of this work.
Keep those gates closed until separately authorized launch checks are complete.

## Assignment, enrollment and delivery

Authenticated `POST /subscriptions/activation-journey/assignment` accepts:

```json
{
  "installation_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  "enrollment_event_id": "11111111-2222-4333-8444-555555555555",
  "enrolled_at_ms": 1788891100110,
  "app_version": "synthetic-version",
  "build_number": "synthetic-build",
  "analytics_environment": "testflight",
  "is_new_installation": true
}
```

The reply contains `journey`, `enrollment_enabled`, `force_standard_paywall`
and `operational_config_version`. A server proposal is **not** an enrollment
denominator. The installation, original enrollment event/time, app/build and
environment are immutable on retry. UUID fields normalize to lowercase; the
new context's exposure ID is strictly `activation_journey_2026_09:<lowercase UUID>`.

An enabled new proposal is deterministically split by installation, default
50/50. Its `assignment_source=server_assignment`, `eligible=true`, and
`randomized=true`. Disabled responses and frozen configuration fallbacks use
the standard search-first experience with both flags false. Explicit local
previews are development-only, with both flags false, and can preview any supported
journey. They never enter randomized production rates.

### September 11 candidate and future allocation

| Immutable variant | Intended onboarding | Intended paywall | Original goals status |
| --- | --- | --- | --- |
| `search_first_standard` | `search_first` | `standard` | `not_asked` |
| `goals_flight_detail` | `goals` | `flight_detail` | `required` |
| `search_first_flight_detail` | `search_first` | `flight_detail` | `not_asked` |

The new candidate goes from welcome to search to the selected-flight-detail
paywall, without a goals question. Its skip-flight surface still uses the
standard paywall/offer. No old goals or standard assignment may acquire this
new meaning, even when replayed after allocation changes or forced-standard
delivery. Both not-asked variants reject final goal submissions and diagnostics
that claim goals were required or confirmed; they do not create empty answers.

An explicit `ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT` setting chooses the
non-standard allocation bucket: `goals_flight_detail` (unchanged default) or
`search_first_flight_detail`. The retained setting name
`ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT` means the **search_first_standard
share**, not the combined share of both search-first variants. Existing
proposals and enrollments are replayed before consulting either setting.

For a future authorized standard-versus-candidate comparison, prepare the
selector as `search_first_flight_detail`, a separately approved standard share
(50 for an equal split), and a distinct `ACTIVATION_JOURNEY_CONFIG_VERSION`
such as `candidate_2026_09_11`. This documentation changes no environment file.
Production and nonproduction enrollment remain **false** by default; changing
the selector alone cannot enroll anyone. Keep the launch closed until the
compatible new client/build and exact UTC/configuration boundary are approved.
Older clients must not receive an unknown variant through a broadly enabled
launch. Validate in separately authorized nonproduction traffic first.

The report retains all three separate variant rows and original build/config
strata. The original standard-minus-goals difference keeps its old identities;
the new candidate-minus-standard difference is explicitly named. These are
descriptive rates for the requested extraction, not proof that all historical
rows were concurrent. Use compatible concurrent launch/build windows before
causal comparisons and never pool goals with the candidate.

The client must persist a common enrollment before showing welcome. A bounded
timeout can freeze its fallback even if a late server proposal exists. The
backend accepts that frozen fallback without overwriting the proposal, making
it randomized, or adopting a later allocation. Any subsequently received
assignment response returns the frozen delivered context. Original entry
context is separate from operational delivery overrides. The default-false
force-standard switch affects only future presentations; it does not change
the original journey or an already frozen checkout.

Authenticated `POST /subscriptions/activation-journey/enrollment` takes
`{"journey": <context>}`. The canonical `activation_journey_enrolled` diagnostic
can deliver the same enrollment independently; it must use the exact stored
enrollment event ID and capture timestamp. Both are idempotent. Other events
can arrive earlier and remain explicitly unjoined; they do not invent entries.

An additive identity reservation serializes new assignment, new metadata, and
old-protocol exposure/enrollment/onboarding writers across SQLite workers.
It binds ownership and prevents a journey installation from entering a legacy
cohort. Known old or in-progress installations cannot newly enroll. Existing
legacy cohort records and report-window definitions are not rewritten.

## Diagnostics, goals and verified purchases

The existing authenticated diagnostic route accepts an optional top-level
`journey` separately from legacy `experiment`. New journey diagnostics omit
the old experiment field, require schema `18` or newer, match installation and
environment, and retain capture-time context. All existing strict property
and privacy allowlists remain in force; raw search queries remain forbidden.

The additional first-selection milestone is
`activation_journey_selected_flight`: `selection_eligible=true` plus the frozen
opaque `flight_identity`, with one immutable event ID for the first eligible
selection. Later result taps are still ordinary `flight_selected` facts. This
first selection supplies discovery/conditional denominators, not the identity
of a potentially different flight purchased after backtracking.

Effective delivery uses bounded `effective_onboarding`, `effective_paywall`,
`effective_offer`, `goals_status`, `operational_override`, and
`paywall_surface=selected_flight|skip_flight|other` properties. Skip-flight
events require the standard paywall and standard offer. Intended assignment,
currently selected product, presentation ID, checkout attempt ID, search
journey and flight identity remain distinct.

The existing `/subscriptions/experiments/goals` route accepts exactly one of
`experiment` or `journey`. New journeys require durable confirmation ID and
monotone revision. The goals journey stores one final ordered answer, including
development preview QA; search-first rejects a goal confirmation instead of
inventing an empty/not-asked answer. Old confirmation receipt hashing omits the
new absent field, preserving pre-upgrade lost-ack retries byte-for-byte.

Transaction registration stays `{jws_payload, experiment?, journey?}`. Verified
transaction, revenue and entitlement facts commit **before** optional journey
contract validation/attribution. Malformed variant/schema/context produces
payment success plus `experiment_tracking_status=conflict`; missing enrollment
or temporary metadata storage failure produces payment success plus `pending`.
The bounded optional journey object is never logged. Invalid Apple JWS remains
invalid, and the existing legacy request contract is preserved.

One separate association binds an original subscription to its original
journey. No second `ExperimentConversion` or financial row is fabricated.
An entitlement restore link is not original checkout ownership: a different
persisted app-account owner cannot claim first-time journey attribution, even
if prior metadata never arrived. Legitimate restored entitlement and verified
financial facts still succeed. Conflicts never move an existing association.

Recovery reporting has a separate, stricter owner gate: the authenticated
account must match the persisted transaction app-account owner and, when a
recovery row already exists, that row's owner. UUID casing or representation
differences are normalized. Missing or malformed ownership returns the same
404 as an unavailable transaction, before resolving, replacing or changing a
flight on the row. A restored entitlement link is not recovery authority;
these checks do not change restore entitlements or verified financial facts.

Historical provenance limitation: existing transaction ingestion can use an
authenticated-account fallback when an Apple transaction lacks an app-account
token. Recovery cannot retroactively distinguish that fallback from an
original signed checkout owner. The existing-row gate preserves an already
recorded owner, but this local change does not reconstruct tokenless historical
ownership or change financial ingestion. New-client captured checkout ownership
and Apple's matching app-account token are the intended recovery contract.

## Protected reporting

Both routes below require the existing administrative Lambda authorization.

- `GET /subscriptions/activation-journey/summary` accepts environment, original
  app/build, inclusive entry start, exclusive entry end and extraction cutoff.
- `GET /subscriptions/activation-journey/baseline-manifest` requires a fixed
  `as_of_ms` and provides bounded keyset pagination via `next_cursor`.

The new summary counts unique eligible randomized installation assignments.
Ten-minute discovery/search and 24-hour paywall/trial/payer rates include
never-searchers and skippers in their mature all-entrant denominators. Recent
entrants are censored, not failures. First-selected-flight trial conversion is
a separately labeled conditional metric. Trial and positive-price nontrial
payments come only from linked verified Apple facts in the matching StoreKit
environment. Native currencies and actual purchased products remain separate.

Correct post-purchase activation joins the first verified activation transaction
to its frozen checkout initiation and verified purchase diagnostic: matching
transaction, actual product, StoreKit environment, attempt, presentation,
surface and flight identity. It then requires that identity's successful save
and subsequent detail presentation within 24 hours of Apple purchase time.
Selecting A, returning, and buying B checks B. Returning and buying through
skip-flight is not a selected-flight activation denominator. Missing/conflicting
checkout joins are unavailable evidence, not inferred failures or reuse of A.

D14/D30 use mature installation cohorts, native-currency actual-product
payments, observed refunds, payer conversion and trial-to-paid. Verified trial
expiry controls trial maturity. Apple's milli-percent refunds use the shared
verified refund calculation. Refund-adjusted customer receipts are not
developer proceeds. Product/currency rows may overlap installations and their
conversion counts must not be summed.

The summary exposes count/rate/Wilson intervals, percentage-point differences,
fallback/configuration/delivery strata, unjoined events and maturity. It caps
entries at 20,000 and each fact extraction at 100,000; exceeding a cap returns
no partial rates. A successful bounded extraction does not prove complete
client delivery. Event-dependent rates become unavailable when diagnostic
retention cannot cover the cohort; retained financial and first-selection
ledgers remain separate. A final goal revision accepted after an old cutoff
is not substituted into the past; its earlier answer is explicitly unknown.

The baseline manifest exports source provenance, installation deduplication
keys, original app/build/environment/time, known revision/configuration and
raw variant. Unknown source revisions remain null. It excludes old activation
control as an onboarding baseline candidate and marks all other compatibility
as unclassified until actual layout/offer/preview/intervention/acquisition
evidence is audited. Later selected-flight enrollment cannot prove a welcome
assignment or reconstruct abandoners. The manifest is not a certified expanded
baseline, and exhausting all pages does not establish historical delivery
coverage. The existing paywall report and frozen 3.7/3.8 boundaries are unchanged.

Reports use a purchase-time cutoff with currently retained verified financial
state, not an immutable historical database snapshot. Delayed verification,
refund facts and late attribution can revise a cohort. Extraction timestamps
and incompleteness must remain visible.

## Verification and remaining launch gates

`tests/test_activation_journey.py` covers default-off/sticky assignment, legacy
boundary, ownership, retry/conflict atomicity, strict schema/privacy, goals,
financial/metadata separation, restores, maturity, backtracking flight joins,
cutoff/retention handling, protected reporting and independent SQLite workers.
`scripts/verify_activation_journey_wire.py` consumes **actual Swift-encoded**
synthetic JSON and validates exact bytes through local DTOs and HTTP routes.

Run tests from a fresh temporary working directory with an empty environment
and explicit repository `PYTHONPATH`; importing the existing backend models
creates a local SQLite database relative to the working directory. Do not run
these checks from a directory containing a real database or production `.env`.

Local passing tests and UI screenshots are not TestFlight or production event
delivery proof. Before enabling: freeze build/configuration/UTC launch scope,
audit and preserve historical baseline/prior-paywall snapshots, perform both
journeys and negative purchase paths with ordered TestFlight backend delivery,
review actual release privacy/AI-consent scope, and obtain explicit launch
authorization. No result here labels a winner. The separately named candidate
is local preparation only, not an enabled extra concurrent arm or a remapping
of either historical journey.
