"""Immutable full-journey metadata; never writes legacy cohorts or revenue facts."""

import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy import func, or_, update
from sqlmodel import Session, select

from ..activation_journey_contract import (
    ActivationJourneyAssignmentRequest, ActivationJourneyContext, JOURNEY_ID, JOURNEY_SCOPES, canonical_context,
)
from ..config import settings
from ..models.activation_journey import (
    ActivationJourneyAssignment, ActivationJourneyAttribution, ActivationJourneyEnrollment,
    ActivationJourneyGoalReceipt, ActivationJourneyGoalSelection,
    ActivationJourneyIdentity, ActivationJourneyDiagnosticContext, ActivationJourneySelection,
)
from ..models.experiment import ExperimentDiagnosticEvent, ExperimentEnrollment, ExperimentExposure, current_time_ms
from ..models.transaction import Transaction

DAY_MS = 86_400_000


def enrollment_id(context):
    return context.exposure_id + ":v1"


def _conflict():
    raise HTTPException(status_code=409, detail="Immutable journey context conflict")


def _owner(row, user):
    if row is not None and row.user_id != user.id:
        raise HTTPException(status_code=403, detail="Journey belongs to another account")


def reserve_identity(*, session, user, identity, context=None):
    session.exec(insert(ActivationJourneyIdentity).values(id=identity, user_id=user.id).on_conflict_do_nothing())
    reservation = session.get(ActivationJourneyIdentity, identity, populate_existing=True)
    _owner(reservation, user)
    if reservation.protocol != "journey":
        raise HTTPException(status_code=409, detail="Existing onboarding installation cannot enter new protocol")
    if context is not None:
        frozen = canonical_context(context)
        session.exec(update(ActivationJourneyIdentity).where(
            ActivationJourneyIdentity.id == identity,
            ActivationJourneyIdentity.frozen_context_json.is_(None),
        ).values(frozen_context_json=frozen))
        if session.get(ActivationJourneyIdentity, identity, populate_existing=True).frozen_context_json != frozen:
            _conflict()


def reserve_legacy_installation(*, session, user, installation_id):
    """One database lock protects the migration boundary across old/new writers.

    This reservation is not a legacy cohort or a new enrollment. Existing old
    protocol ownership and source records retain their original semantics.
    """
    identity = f"{JOURNEY_ID}:{installation_id}:v1"
    session.exec(insert(ActivationJourneyIdentity).values(
        id=identity, user_id=user.id, protocol="legacy",
    ).on_conflict_do_nothing())
    row = session.get(ActivationJourneyIdentity, identity, populate_existing=True)
    if row.protocol != "legacy":
        raise HTTPException(status_code=409, detail="Journey installation cannot enter a legacy cohort")


def _cohort_boundary_onboarding_events(installation):
    """Exclude explicitly cohortless schema-20 capture, preserve older evidence.

    Standalone operational telemetry must not reserve or imply an experiment
    protocol, while old unversioned onboarding facts keep their migration role.
    Explicit old/new cohort context still follows the existing owner checks.
    """
    return select(ExperimentDiagnosticEvent).where(
        ExperimentDiagnosticEvent.installation_id == installation,
        ExperimentDiagnosticEvent.event_name.in_(("onboarding_started", "onboarding_completed")),
        or_(
            ExperimentDiagnosticEvent.experiment_id.is_not(None),
            func.json_extract(ExperimentDiagnosticEvent.properties_json, "$._activation_journey").is_not(None),
            func.coalesce(func.json_extract(ExperimentDiagnosticEvent.properties_json, "$.event_schema_version"), 0) < 20,
        ),
    )


def _known_legacy_installation(session, installation):
    # An old selected-flight/exposure row is sufficient to refuse new allocation,
    # never sufficient to invent a historical welcome or full-journey assignment.
    for model in (ExperimentExposure, ExperimentEnrollment):
        if session.exec(select(model.id).where(model.installation_id == installation).limit(1)).first():
            return True
    return session.exec(_cohort_boundary_onboarding_events(installation).limit(1)).first() is not None


def _request_matches_context(request, context):
    return all(getattr(request, key) == getattr(context, key) for key in (
        "installation_id", "enrollment_event_id", "enrolled_at_ms", "app_version",
        "build_number", "analytics_environment",
    ))


def _assignment_response(context):
    return {"journey": context, "enrollment_enabled": context.eligible,
            "force_standard_paywall": settings.ACTIVATION_JOURNEY_FORCE_STANDARD_PAYWALL,
            "operational_config_version": settings.ACTIVATION_JOURNEY_OPERATIONAL_CONFIG_VERSION}


def assign_journey(*, session: Session, user, data: ActivationJourneyAssignmentRequest):
    identity = f"{JOURNEY_ID}:{data.installation_id}:v1"
    reserve_identity(session=session, user=user, identity=identity)
    frozen = session.get(ActivationJourneyIdentity, identity, populate_existing=True).frozen_context_json
    if frozen:
        context = ActivationJourneyContext.model_validate_json(frozen)
        if not _request_matches_context(data, context):
            _conflict()
        return _assignment_response(context)
    existing_entry = session.get(ActivationJourneyEnrollment, identity, populate_existing=True)
    if existing_entry:
        _owner(existing_entry, user)
        context = ActivationJourneyContext.model_validate_json(existing_entry.context_json)
        if not _request_matches_context(data, context):
            _conflict()
        return _assignment_response(context)
    existing = session.get(ActivationJourneyAssignment, identity, populate_existing=True)
    if existing:
        _owner(existing, user)
        if existing.request_json != data.model_dump_json():
            _conflict()
        context = ActivationJourneyContext.model_validate_json(existing.context_json)
        return _assignment_response(context)
    if _known_legacy_installation(session, str(data.installation_id)):
        raise HTTPException(status_code=409, detail="Existing onboarding installation cannot enter new protocol")
    now = current_time_ms()
    if not now - DAY_MS <= data.enrolled_at_ms <= now + 300_000:
        raise HTTPException(status_code=422, detail="New assignment timestamp is outside the allowed window")
    enabled = (settings.ACTIVATION_JOURNEY_PRODUCTION_ENROLLMENT_ENABLED
               if data.analytics_environment == "production"
               else settings.ACTIVATION_JOURNEY_NONPRODUCTION_ENROLLMENT_ENABLED)
    percent = min(100, max(0, settings.ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT))
    bucket = int.from_bytes(hashlib.sha256(f"{JOURNEY_ID}:{data.installation_id}".encode()).digest()[:8], "big") % 100
    variant = ("search_first_standard" if not enabled or bucket < percent
               else settings.ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT)
    intended_onboarding, intended_paywall, goals_status = JOURNEY_SCOPES[variant]
    context = ActivationJourneyContext(
        experiment_id=JOURNEY_ID, measurement_revision=1, variant=variant,
        eligible=enabled, randomized=enabled, installation_id=data.installation_id,
        exposure_id=f"{JOURNEY_ID}:{data.installation_id}", enrollment_event_id=data.enrollment_event_id,
        enrolled_at_ms=data.enrolled_at_ms, app_version=data.app_version, build_number=data.build_number,
        analytics_environment=data.analytics_environment,
        assignment_source="server_assignment" if enabled else "server_disabled",
        config_version=settings.ACTIVATION_JOURNEY_CONFIG_VERSION,
        intended_onboarding=intended_onboarding,
        intended_paywall=intended_paywall,
        goals_status=goals_status,
    )
    # Atomic first proposal wins across workers/config changes. Replays return
    # that proposal, never a newly computed arm or new capture timestamp.
    session.exec(insert(ActivationJourneyAssignment).values(
        id=identity, installation_id=str(data.installation_id), user_id=user.id,
        context_json=canonical_context(context), request_json=data.model_dump_json(), created_at_ms=now,
    ).on_conflict_do_nothing())
    stored = session.get(ActivationJourneyAssignment, identity, populate_existing=True)
    _owner(stored, user)
    if stored.request_json != data.model_dump_json():
        _conflict()
    context = ActivationJourneyContext.model_validate_json(stored.context_json)
    return _assignment_response(context)


def validate_journey(*, session: Session, user, context: ActivationJourneyContext, require_enrollment=False):
    identity = enrollment_id(context)
    reservation = session.get(ActivationJourneyIdentity, identity, populate_existing=True)
    _owner(reservation, user)
    if reservation and reservation.protocol != "journey":
        _conflict()
    if reservation and reservation.frozen_context_json and reservation.frozen_context_json != canonical_context(context):
        _conflict()
    entry = session.get(ActivationJourneyEnrollment, identity, populate_existing=True)
    _owner(entry, user)
    if entry:
        if entry.context_json != canonical_context(context):
            _conflict()
        return entry
    proposal = session.get(ActivationJourneyAssignment, identity, populate_existing=True)
    _owner(proposal, user)
    if context.assignment_source in ("server_assignment", "server_disabled"):
        if proposal is None:
            raise HTTPException(status_code=425, detail="Original server assignment is unavailable")
        if proposal.context_json != canonical_context(context):
            _conflict()
    elif proposal:
        # A bounded request may have timed out after the server wrote a proposal.
        # Persist the client's already-frozen fallback, not a late reallocation.
        request = ActivationJourneyAssignmentRequest.model_validate_json(proposal.request_json)
        if not _request_matches_context(request, context):
            _conflict()
    if require_enrollment:
        raise HTTPException(status_code=425, detail="Canonical journey enrollment has not arrived")
    return None


def enroll_journey(*, session: Session, user, context: ActivationJourneyContext):
    reserve_identity(session=session, user=user, identity=enrollment_id(context), context=context)
    existing = validate_journey(session=session, user=user, context=context)
    if existing:
        return existing
    if _known_legacy_installation(session, str(context.installation_id)):
        # A diagnostic for this *new* journey may arrive first; only existing
        # rows carrying the same validated journey escape the legacy-start guard.
        legacy = session.exec(select(ExperimentExposure.id).where(ExperimentExposure.installation_id == str(context.installation_id)).limit(1)).first()
        old_enrollment = session.exec(select(ExperimentEnrollment.id).where(ExperimentEnrollment.installation_id == str(context.installation_id)).limit(1)).first()
        starts = session.exec(_cohort_boundary_onboarding_events(str(context.installation_id))).all()
        if legacy or old_enrollment or any(json.loads(row.properties_json).get("_activation_journey") != context.model_dump(mode="json") for row in starts):
            raise HTTPException(status_code=409, detail="Existing onboarding installation cannot enter new protocol")
    now = current_time_ms()
    if not now - 90 * DAY_MS <= context.enrolled_at_ms <= now + 300_000:
        raise HTTPException(status_code=422, detail="Enrollment timestamp is outside the allowed window")
    values = dict(
        id=enrollment_id(context), experiment_id=JOURNEY_ID, measurement_revision=1,
        installation_id=str(context.installation_id), user_id=user.id, variant=context.variant,
        eligible=context.eligible, randomized=context.randomized, app_version=context.app_version,
        build_number=context.build_number, analytics_environment=context.analytics_environment,
        enrolled_at_ms=context.enrolled_at_ms, enrollment_event_id=str(context.enrollment_event_id),
        assignment_source=context.assignment_source, config_version=context.config_version,
        context_json=canonical_context(context), first_reported_at_ms=now,
    )
    session.exec(insert(ActivationJourneyEnrollment).values(**values).on_conflict_do_nothing())
    stored = session.get(ActivationJourneyEnrollment, values["id"], populate_existing=True)
    if stored is None:
        _conflict()
    _owner(stored, user)
    if stored.context_json != values["context_json"]:
        _conflict()
    return stored


def _same_attribution_owner(owner_id, user_id):
    if not owner_id or not user_id:
        return False
    try:
        return UUID(owner_id) == UUID(user_id)
    except (ValueError, TypeError, AttributeError):
        # Preserve exact historical non-UUID account identifiers. Invalid or
        # missing provenance never gains equivalence to a different account.
        return owner_id == user_id


def attribute_journey_transaction(*, session: Session, user, context, transaction_id):
    entry = validate_journey(session=session, user=user, context=context, require_enrollment=True)
    transaction = session.get(Transaction, str(transaction_id), populate_existing=True)
    if not transaction:
        raise HTTPException(status_code=404, detail="Owned verified transaction unavailable")
    # A restore legitimately links entitlement to another account, but that new
    # link is not proof that the restoring journey owned the original checkout.
    if not _same_attribution_owner(transaction.app_account_token, user.id):
        raise HTTPException(status_code=403, detail="Verified checkout belongs to another original account")
    if transaction.purchase_date is None or transaction.original_purchase_date is None:
        raise HTTPException(status_code=425, detail="Original verified purchase timing unavailable")
    if min(transaction.purchase_date, transaction.original_purchase_date) < entry.enrolled_at_ms:
        _conflict()
    expected_environment = "Production" if context.analytics_environment == "production" else "Sandbox" if context.analytics_environment == "testflight" else None
    environment = getattr(transaction.environment, "value", transaction.environment)
    if (expected_environment and environment != expected_environment) or (expected_environment is None and environment not in ("Sandbox", "Xcode")):
        _conflict()
    values = dict(id=transaction.subscription_id, enrollment_id=entry.id, user_id=user.id,
                  first_transaction_id=transaction.id, context_json=entry.context_json,
                  attributed_at_ms=current_time_ms())
    session.exec(insert(ActivationJourneyAttribution).values(**values).on_conflict_do_nothing())
    stored = session.get(ActivationJourneyAttribution, transaction.subscription_id, populate_existing=True)
    if stored.user_id != user.id or stored.enrollment_id != entry.id or stored.context_json != entry.context_json:
        _conflict()
    return stored


def confirm_journey_goals(*, session: Session, user, data):
    context = data.journey
    if context.variant != "goals_flight_detail" or context.goals_status != "required":
        raise HTTPException(status_code=422, detail="Goals were not asked in this journey")
    entry = validate_journey(session=session, user=user, context=context, require_enrollment=True)
    if not context.enrolled_at_ms <= data.selected_at_ms <= current_time_ms() + 300_000:
        raise HTTPException(status_code=422, detail="Goal confirmation timing conflicts with journey")
    payload = json.dumps(data.model_dump(mode="json", exclude_none=True), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    receipt_id = f"{entry.id}:{data.confirmation_id}"
    # The first write serializes all subsequent receipt and revision checks.
    session.exec(insert(ActivationJourneyGoalReceipt).values(
        id=receipt_id, enrollment_id=entry.id, user_id=user.id,
        confirmation_revision=data.confirmation_revision, payload_sha256=digest,
    ).on_conflict_do_nothing())
    receipt = session.get(ActivationJourneyGoalReceipt, receipt_id, populate_existing=True)
    if receipt is None or receipt.payload_sha256 != digest or receipt.user_id != user.id or receipt.confirmation_revision != data.confirmation_revision:
        _conflict()
    values = dict(id=entry.id, user_id=user.id, confirmation_revision=data.confirmation_revision,
                  confirmation_id=str(data.confirmation_id), selected_at_ms=data.selected_at_ms,
                  selected_goal_keys=",".join(data.selected_goal_keys), payload_sha256=digest,
                  reported_at_ms=current_time_ms())
    statement = insert(ActivationJourneyGoalSelection).values(**values)
    accepted = session.exec(statement.on_conflict_do_update(
        index_elements=["id"], set_=values,
        where=statement.excluded.confirmation_revision > ActivationJourneyGoalSelection.confirmation_revision,
    ).returning(ActivationJourneyGoalSelection.id)).scalar_one_or_none() is not None
    current = session.get(ActivationJourneyGoalSelection, entry.id, populate_existing=True)
    outcome = "accepted" if accepted else "idempotent" if current.payload_sha256 == digest else "stale"
    return dict(detail="success", status=outcome, request_confirmation_id=str(data.confirmation_id),
                accepted_revision=current.confirmation_revision, accepted_confirmation_id=current.confirmation_id)


def record_journey_diagnostic(*, session, user, event):
    context = event.journey
    identity = enrollment_id(context)
    reserve_identity(session=session, user=user, identity=identity, context=context)
    validate_journey(session=session, user=user, context=context)
    if event.event_name == "activation_journey_enrolled":
        enroll_journey(session=session, user=user, context=context)
    session.exec(insert(ActivationJourneyDiagnosticContext).values(
        id=str(event.event_id), enrollment_id=identity, user_id=user.id,
        context_json=canonical_context(context),
    ).on_conflict_do_nothing())
    if event.event_name == "activation_journey_selected_flight":
        values = dict(id=identity, event_id=str(event.event_id), user_id=user.id,
                      selected_at_ms=event.occurred_at_ms, flight_identity=event.properties.flight_identity,
                      flight_id=event.properties.flight_id)
        session.exec(insert(ActivationJourneySelection).values(**values).on_conflict_do_nothing())
        stored = session.get(ActivationJourneySelection, identity, populate_existing=True)
        if stored is None or stored.model_dump(exclude={"first_reported_at_ms"}) != values:
            raise HTTPException(status_code=409, detail="First eligible selection already has different facts")
