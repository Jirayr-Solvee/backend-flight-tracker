"""Authenticated, bounded diagnostics; never a source of verified revenue."""

import json
from collections import Counter, defaultdict
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..dependency import check_lambda_auth_token, get_current_user
from ..models import get_session
from ..models.experiment import ExperimentDiagnosticEvent, current_time_ms
from ..models.transaction import Transaction
from ..models.user import User, UserSubscriptionLink
from .subscriptions import ExperimentContext
from ..activation_journey_contract import ActivationJourneyContext
from ..models.activation_journey import ActivationJourneyDiagnosticContext
from ..services.activation_journey import record_journey_diagnostic, validate_journey, reserve_legacy_installation
from ..services.notification_analytics import seal_copy, open_copy

router = APIRouter()
Token = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")]
ProductID = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.-]+$")]
FlightIdentity = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")]
SETTING_VALUES = {
    "distance_unit": frozenset(("km", "mi")),
    "time_format": frozenset(("12_hour", "24_hour")),
    "history_sort": frozenset(("date_ascending", "date_descending", "airline_ascending", "airline_descending",
                                "departure_ascending", "departure_descending", "arrival_ascending", "arrival_descending")),
}
EventName = Literal[
    "app_launched", "app_became_active", "app_became_inactive",
    "apns_registered", "apns_registration_failed", "push_received", "push_opened",
    "screen_viewed", "screen_home_viewed", "screen_flight_search_viewed",
    "screen_flight_search_results_viewed", "screen_history_viewed", "screen_profile_viewed",
    "screen_copilot_viewed", "screen_arrival_summary_viewed",
    "activation_journey_enrolled", "activation_journey_selected_flight",
    "activation_experiment_exposed", "pricing_experiment_exposed", "onboarding_choice_selected",
    "paywall_viewed", "paywall_dismissed", "flight_detail_paywall_experiment_exposed",
    "flight_detail_paywall_experiment_enrolled",
    "subscription_product_selected", "af_initiated_checkout", "checkout_attempt_completed",
    "af_start_trial", "af_purchase", "purchase_cancelled", "purchase_pending",
    "purchase_unverified", "purchase_error", "flight_selected", "flight_added",
    "subscription_restore_started", "subscription_restore_completed", "subscription_restore_failed",
    "screen_flight_detail_viewed", "post_purchase_flight_activation",
    "notification_permission_result", "tracking_briefing_scheduled",
    "flight_notification_scheduling", "live_activity_started", "live_activity_start_failed",
    "notification_deep_link_opened", "post_arrival_follow_up_action", "live_activity_opened",
    "live_activity_push_to_start_registration",
    "live_activity_update_registration",
    "flight_add_blocked", "flight_add_failed", "transaction_registration_outcome",
    "flight_deleted", "flight_deletion_outcome", "global_flight_tapped", "global_flight_resolved", "global_flight_discarded",
    "copilot_tapped", "copilot_opened", "copilot_telemetry_loaded",
    "delay_risk_loaded", "delay_risk_failed", "arrival_card_viewed", "arrival_card_action",
    "review_prompt_requested", "voice_search_action", "permission_result", "setting_changed",
    "search_suggestion_selected", "account_action", "flight_import_action",
    "paywall_alternative_plans_revealed", "paywall_products_loaded",
    "af_search", "search_completed", "search_failed", "no_search_results",
    "search_recovery_shown", "search_recovery_suggestion_selected",
    "onboarding_started", "onboarding_step_viewed", "onboarding_completed", "activation_experiment_action", "experience_action",
]


class DiagnosticProperties(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    experience_presentation_id: Annotated[UUID, Field(strict=False)] | None = None
    message_id: Token | None = None
    message_category: Literal["All", "Weather", "Diversion", "Crew", "Cargo", "Cabin", "Operations"] | None = None
    message_count: Annotated[int, Field(ge=0, le=100000)] | None = None
    product_id: ProductID | None = None
    displayed_product_id: ProductID | None = None
    assigned_product_id: ProductID | None = None
    purchase_environment: Literal["Production", "Sandbox", "Xcode", "unknown"] | None = None
    selection_method: Literal["default", "user"] | None = None
    outcome: Token | None = None
    source: Token | None = None
    reason: Token | None = None
    stage: Token | None = None
    effective_variant: Token | None = None
    assignment_source: Token | None = None
    config_version: Token | None = None
    offer_eligibility: Literal["eligible", "ineligible", "unknown"] | None = None
    offer_context: Token | None = None
    used_legacy_fallback: bool | None = None
    starts_trial: bool | None = None
    transaction_id: Token | None = None
    original_transaction_id: Token | None = None
    flight_id: Annotated[int, Field(ge=1)] | None = None
    flight_identity: FlightIdentity | None = None
    trial_duration_days: Annotated[int, Field(ge=0, le=365)] | None = None
    event_schema_version: Annotated[int, Field(ge=1, le=1000)] | None = None
    attempt_count: Annotated[int, Field(ge=0, le=1000)] | None = None
    notification_type: Token | None = None
    notification_id: Token | None = None
    notification_copy_id: FlightIdentity | None = None
    notification_open_delay: Literal["under_1m", "1m_to_1h", "1h_to_1d", "over_1d"] | None = None
    status: Token | None = None
    activity_kind: Token | None = None
    search_journey_id: Annotated[UUID, Field(strict=False)] | None = None
    search_attempt_number: Annotated[int, Field(ge=1, le=1000)] | None = None
    session_id: Annotated[UUID, Field(strict=False)] | None = None
    visible_plan_count: Annotated[int, Field(ge=0, le=20)] | None = None
    available_plan_count: Annotated[int, Field(ge=0, le=20)] | None = None
    mode: Token | None = None
    query_type: Token | None = None
    failure_reason: Token | None = None
    provider_outcome: Token | None = None
    provider_latency_bucket: Token | None = None
    suggestion_kind: Token | None = None
    action: Token | None = None
    step_key: Token | None = None
    has_results: bool | None = None
    normalization_applied: bool | None = None
    flight_count: Annotated[int, Field(ge=0, le=100000)] | None = None
    airport_flight_count: Annotated[int, Field(ge=0, le=100000)] | None = None
    provider_result_count: Annotated[int, Field(ge=0, le=100000)] | None = None
    filtered_result_count: Annotated[int, Field(ge=0, le=100000)] | None = None
    suggestion_count: Annotated[int, Field(ge=0, le=1000)] | None = None
    step: Annotated[int, Field(ge=0, le=100)] | None = None
    total_steps: Annotated[int, Field(ge=0, le=100)] | None = None
    effective_onboarding: Literal["search_first", "goals"] | None = None
    effective_paywall: Literal["standard", "flight_detail"] | None = None
    effective_offer: Literal["standard", "flight_detail_treatment"] | None = None
    goals_status: Literal["not_asked", "required", "confirmed"] | None = None
    paywall_surface: Literal["selected_flight", "skip_flight", "airport", "other"] | None = None
    operational_override: Literal["none", "forced_standard", "configuration_fallback"] | None = None
    selection_eligible: bool | None = None
    # Schema 20 extends the operational projection, never arbitrary analytics
    # dictionaries. Human labels, queries, routes, callsigns and payload bodies
    # remain absent; these values are machine-readable bounded codes only.
    screen: Token | None = None
    app_language: Token | None = None
    permission: Literal["speech", "microphone", "tracking", "location"] | None = None
    setting: Literal["distance_unit", "time_format", "history_sort"] | None = None
    value: Literal["km", "mi", "12_hour", "24_hour", "date_ascending", "date_descending",
                   "airline_ascending", "airline_descending", "departure_ascending", "departure_descending",
                   "arrival_ascending", "arrival_descending"] | None = None
    choice_key: Token | None = None
    update_type: Token | None = None
    level: Token | None = None
    confidence: Token | None = None
    exposure_scope: Token | None = None
    enrollment_scope: Token | None = None
    completion_semantics: Token | None = None
    selection_stage: Token | None = None
    view_scope: Token | None = None
    load_scope: Token | None = None
    layout: Token | None = None
    has_transcript: bool | None = None
    has_active_entitlement: bool | None = None
    has_flight_context: bool | None = None
    has_live_telemetry: bool | None = None
    shows_summary_action: bool | None = None
    selected: bool | None = None
    selected_count: Annotated[int, Field(ge=0, le=20)] | None = None
    score: Annotated[int, Field(ge=0, le=100)] | None = None
    measurement_revision: Annotated[int, Field(ge=1, le=100)] | None = None
    source_flight_id: Annotated[int, Field(ge=1, le=9_223_372_036_854_775_807)] | None = None
    new_flight_id: Annotated[int, Field(ge=1, le=9_223_372_036_854_775_807)] | None = None


class NotificationContent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(max_length=512)
    subtitle: str = Field(default="", max_length=512)
    body: str = Field(max_length=4096)


class DiagnosticEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    event_name: EventName
    occurred_at_ms: int = Field(ge=0)
    installation_id: UUID
    app_version: Token
    build_number: Token
    analytics_environment: Literal["production", "development", "testflight"]
    build_configuration: Literal["debug", "release"]
    paywall_presentation_id: UUID | None = None
    checkout_attempt_id: UUID | None = None
    experiment: ExperimentContext | None = None
    journey: ActivationJourneyContext | None = None
    properties: DiagnosticProperties = Field(default_factory=DiagnosticProperties)
    notification_content: NotificationContent | None = None

    @model_validator(mode="after")
    def validate_context(self):
        if self.notification_content is not None and (
            self.event_name != "push_opened" or (self.properties.event_schema_version or 0) < 24
            or not self.properties.notification_id or not self.properties.notification_copy_id
            or self.properties.source not in ("remote", "local") or self.properties.action != "default_tap"
        ):
            raise ValueError("Notification copy requires a schema-24 notification tap")
        if self.build_configuration == "debug" and self.analytics_environment != "development":
            raise ValueError("Debug diagnostics must use development environment")
        if self.experiment and (
            self.experiment.installation_id != self.installation_id
            or self.experiment.analytics_environment != self.analytics_environment
        ):
            raise ValueError("Experiment and event context must match")
        if self.journey:
            if self.experiment is not None:
                raise ValueError("New journey diagnostics must not create legacy experiment facts")
            if self.journey.installation_id != self.installation_id or self.journey.analytics_environment != self.analytics_environment:
                raise ValueError("Journey and event context must match")
            if self.occurred_at_ms < self.journey.enrolled_at_ms or (self.properties.event_schema_version or 0) < 18:
                raise ValueError("Journey events require capture-time schema 18 and valid timing")
            if self.event_name in ("flight_detail_paywall_experiment_exposed", "flight_detail_paywall_experiment_enrolled"):
                raise ValueError("Legacy cohort events cannot enroll a new journey")
            if self.properties.paywall_surface == "skip_flight" and (
                self.properties.effective_offer != "standard" or self.properties.effective_paywall != "standard"
            ):
                raise ValueError("Skip-flight uses the standard offer and paywall")
            if self.journey.goals_status == "not_asked" and self.properties.goals_status not in (None, "not_asked"):
                raise ValueError("A search-first journey cannot claim asked or confirmed goals")
        if self.event_name in ("activation_journey_enrolled", "activation_journey_selected_flight"):
            if self.journey is None:
                raise ValueError("Canonical journey milestones require journey context")
        if self.event_name == "activation_journey_enrolled" and (
            self.event_id != self.journey.enrollment_event_id or self.occurred_at_ms != self.journey.enrolled_at_ms
        ):
            raise ValueError("Canonical enrollment ID and capture time must match")
        if self.event_name == "activation_journey_selected_flight" and (
            self.properties.selection_eligible is not True or self.properties.flight_identity is None
        ):
            raise ValueError("First eligible selection requires a frozen flight identity")
        if self.event_name in (
            "paywall_viewed", "paywall_dismissed", "subscription_product_selected",
            "af_initiated_checkout", "checkout_attempt_completed",
            "paywall_alternative_plans_revealed", "paywall_products_loaded",
        ) and self.paywall_presentation_id is None:
            raise ValueError("Paywall presentation ID is required")
        if self.event_name in ("af_initiated_checkout", "checkout_attempt_completed"):
            if self.checkout_attempt_id is None:
                raise ValueError("Checkout attempt ID is required")
        if self.event_name == "checkout_attempt_completed" and self.properties.outcome not in (
            "verified", "cancelled", "pending", "unverified", "error",
        ):
            raise ValueError("Invalid checkout terminal outcome")
        # These new schema-20 event names have no historical loose payload to
        # preserve. Require their minimal operational meaning, not free text.
        required = {
            "voice_search_action": ("action", "source", "has_transcript"),
            "permission_result": ("permission", "status", "source"),
            "setting_changed": ("setting", "value", "source"),
            "search_suggestion_selected": ("suggestion_kind", "source"),
            "account_action": ("action", "outcome", "source"),
            "flight_import_action": ("action", "outcome", "source"),
            "flight_deletion_outcome": ("flight_id", "stage", "outcome"),
        }.get(self.event_name, ())
        if required and (
            (self.properties.event_schema_version or 0) < 20
            or any(getattr(self.properties, key) is None for key in required)
        ):
            raise ValueError("Operational event requires schema 20 and its bounded context")
        if self.event_name == "setting_changed" and self.properties.value not in SETTING_VALUES[self.properties.setting]:
            raise ValueError("Setting value does not match its finite setting contract")
        if self.event_name == "permission_result" and self.properties.status not in (
            "authorized", "denied", "restricted", "not_determined", "unknown", "priming_declined",
        ):
            raise ValueError("Permission result must use a fixed authorization state")
        if self.event_name == "voice_search_action" and (
            self.properties.action not in ("microphone_tapped", "priming_shown", "priming_declined",
                                           "recording_started", "recording_stopped", "submitted", "failed")
            or self.properties.reason not in (None, "user", "submitted", "dismissed", "interrupted", "completed",
                                              "recognition_failed", "speech_denied", "microphone_denied",
                                              "recognizer_unavailable", "invalid_audio_format", "audio_start_failed")
        ):
            raise ValueError("Voice diagnostics must use fixed action and failure codes")
        if self.event_name == "search_suggestion_selected" and self.properties.suggestion_kind != "example":
            raise ValueError("Example-chip diagnostics must not include suggestion text")
        if self.event_name == "account_action" and (
            self.properties.action not in ("guest_create", "apple_sign_in", "sign_out", "account_delete")
            or self.properties.outcome not in ("started", "succeeded", "cancelled", "failed")
        ):
            raise ValueError("Account diagnostics must use fixed action and outcome codes")
        if self.event_name == "flight_import_action" and (
            self.properties.action != "forwarding_address_share"
            or self.properties.outcome not in ("presented", "completed", "cancelled", "failed")
        ):
            raise ValueError("Forwarding share-sheet diagnostics are not verified flight import")
        if self.event_name == "flight_deletion_outcome" and (self.properties.stage, self.properties.outcome) not in (
            ("local_save", "succeeded"), ("local_save", "failed"),
            ("backend_delete", "succeeded"), ("backend_delete", "failed"), ("backend_delete", "skipped"),
        ):
            raise ValueError("Flight deletion diagnostics require a fixed persistence stage and outcome")
        return self


class DiagnosticBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[DiagnosticEvent] = Field(min_length=1, max_length=25)


def _row(event: DiagnosticEvent, user: User) -> ExperimentDiagnosticEvent:
    properties = event.properties.model_dump(mode="json", exclude_none=True)
    if event.notification_content is not None:
        encrypted, digest = seal_copy(event.notification_content.model_dump())
        properties["_notification_copy_ciphertext"] = encrypted
        properties["_notification_copy_digest"] = digest
    if event.journey:
        properties["_activation_journey"] = event.journey.model_dump(mode="json")
    return ExperimentDiagnosticEvent(
        id=str(event.event_id), user_id=user.id,
        installation_id=str(event.installation_id), event_name=event.event_name,
        occurred_at_ms=event.occurred_at_ms, app_version=event.app_version,
        build_number=event.build_number, analytics_environment=event.analytics_environment,
        build_configuration=event.build_configuration,
        paywall_presentation_id=str(event.paywall_presentation_id) if event.paywall_presentation_id else None,
        checkout_attempt_id=str(event.checkout_attempt_id) if event.checkout_attempt_id else None,
        experiment_id=event.experiment.experiment_id if event.experiment else None,
        variant=event.experiment.variant if event.experiment else None,
        measurement_revision=event.experiment.measurement_revision if event.experiment else None,
        properties_json=json.dumps(properties, sort_keys=True, separators=(",", ":")),
    )


def _facts(row):
    facts = row.model_dump(exclude={"received_at_ms", "properties_json"})
    properties = json.loads(row.properties_json)
    # Random encryption nonces must not make an identical stable-ID retry conflict.
    properties.pop("_notification_copy_ciphertext", None)
    facts["properties"] = properties
    return facts


@router.post("/experiments/events")
def record_diagnostic_events(
    data: DiagnosticBatch,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    now_ms = current_time_ms()
    incoming = {}
    duplicates = 0
    # Validate the entire batch before making any writes. Stable IDs make a
    # durable client retry safe after backgrounding or a lost response.
    for event in data.events:
        if not now_ms - 90 * 86_400_000 <= event.occurred_at_ms <= now_ms + 86_400_000:
            raise HTTPException(status_code=422, detail="Event timestamp is outside the diagnostic window")
        if event.journey:
            validate_journey(session=session, user=user, context=event.journey)
        row = _row(event, user)
        existing = incoming.get(row.id) or session.get(ExperimentDiagnosticEvent, row.id)
        if existing:
            if _facts(existing) != _facts(row):
                raise HTTPException(status_code=409, detail="Event ID already has different facts")
            duplicates += 1
            continue
        incoming[row.id] = row

    recent_count = session.exec(select(func.count()).select_from(ExperimentDiagnosticEvent).where(
        ExperimentDiagnosticEvent.user_id == user.id,
        ExperimentDiagnosticEvent.received_at_ms >= now_ms - 86_400_000,
    )).one()
    if recent_count + len(incoming) > 10_000:
        raise HTTPException(status_code=429, detail="Diagnostic event daily limit reached")

    for row in incoming.values():
        if row.event_name != "checkout_attempt_completed":
            continue
        existing_terminal = session.exec(select(ExperimentDiagnosticEvent).where(
            ExperimentDiagnosticEvent.user_id == user.id,
            ExperimentDiagnosticEvent.checkout_attempt_id == row.checkout_attempt_id,
            ExperimentDiagnosticEvent.event_name == "checkout_attempt_completed",
        )).first()
        batch_terminals = [value for value in incoming.values()
                           if value.checkout_attempt_id == row.checkout_attempt_id
                           and value.event_name == "checkout_attempt_completed"]
        if existing_terminal is not None or len(batch_terminals) > 1:
            raise HTTPException(status_code=409, detail="Checkout attempt already has a terminal event")
    try:
        for row in incoming.values():
            session.add(row)
        session.flush()
        for event in data.events:
            if event.journey:
                record_journey_diagnostic(session=session, user=user, event=event)
            elif event.event_name in ("onboarding_started", "onboarding_completed", "flight_detail_paywall_experiment_exposed", "flight_detail_paywall_experiment_enrolled") and (
                event.experiment is not None or (event.properties.event_schema_version or 0) < 20
            ):
                # Schema-20 lifecycle/QA capture can intentionally have a random
                # diagnostics installation ID but no cohort. It is not evidence
                # of an old protocol entry. Keep historical pre-20 behavior.
                reserve_legacy_installation(session=session, user=user, installation_id=event.installation_id)
        expired_ids = select(ExperimentDiagnosticEvent.id).where(
            ExperimentDiagnosticEvent.received_at_ms < now_ms - 90 * 86_400_000,
        )
        session.exec(delete(ActivationJourneyDiagnosticContext).where(ActivationJourneyDiagnosticContext.id.in_(expired_ids)))
        session.exec(delete(ExperimentDiagnosticEvent).where(
            ExperimentDiagnosticEvent.received_at_ms < now_ms - 90 * 86_400_000,
        ))
        session.commit()
    except IntegrityError:
        session.rollback()
        # A concurrent exact retry may win the primary key race. Confirm all
        # original facts before acknowledging it; a second terminal is rejected
        # by the database's partial unique index even under concurrent requests.
        for row in incoming.values():
            existing = session.get(ExperimentDiagnosticEvent, row.id)
            if not existing or _facts(existing) != _facts(row):
                raise HTTPException(status_code=409, detail="Concurrent diagnostic facts conflict")
        return {"detail": "success", "accepted": 0, "duplicates": len(data.events)}
    except Exception:
        session.rollback()
        raise
    return {"detail": "success", "accepted": len(incoming), "duplicates": duplicates}


@router.get("/experiments/events/report", dependencies=[Depends(check_lambda_auth_token)])
def get_diagnostic_report(
    installation_id: UUID | None = None,
    analytics_environment: Literal["production", "development", "testflight"] = "production",
    since_ms: int | None = None,
    until_ms: int | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
    session: Session = Depends(get_session),
):
    statement = select(ExperimentDiagnosticEvent).where(
        ExperimentDiagnosticEvent.analytics_environment == analytics_environment,
    )
    if installation_id is not None:
        statement = statement.where(ExperimentDiagnosticEvent.installation_id == str(installation_id))
    if since_ms is not None:
        statement = statement.where(ExperimentDiagnosticEvent.occurred_at_ms >= since_ms)
    if until_ms is not None:
        statement = statement.where(ExperimentDiagnosticEvent.occurred_at_ms < until_ms)
    rows = session.exec(statement.order_by(
        ExperimentDiagnosticEvent.occurred_at_ms.desc(), ExperimentDiagnosticEvent.id
    ).limit(limit + 1)).all()
    truncated = len(rows) > limit
    rows = sorted(rows[:limit], key=lambda item: (item.occurred_at_ms, item.id))
    attempts = defaultdict(list)
    presentations = defaultdict(list)
    journeys = defaultdict(list)
    identities = defaultdict(list)
    properties_by_id = {}
    events = []
    for row in rows:
        properties = json.loads(row.properties_json)
        notification_copy = open_copy(properties.pop("_notification_copy_ciphertext", None))
        properties.pop("_notification_copy_digest", None)
        journey_context = properties.pop("_activation_journey", None)
        properties_by_id[row.id] = properties
        if row.checkout_attempt_id:
            attempts[row.checkout_attempt_id].append(row)
        if row.paywall_presentation_id:
            presentations[row.paywall_presentation_id].append(row)
        if properties.get("search_journey_id"):
            journeys[(row.installation_id, properties["search_journey_id"])].append(row)
        if properties.get("flight_identity"):
            identities[(row.installation_id, properties["flight_identity"])].append(row)
        transaction = session.get(Transaction, properties.get("transaction_id")) if properties.get("transaction_id") else None
        owned_transaction = bool(transaction and (
            transaction.app_account_token == row.user_id
            or session.exec(select(UserSubscriptionLink).where(
                UserSubscriptionLink.user_id == row.user_id,
                UserSubscriptionLink.subscription_id == transaction.subscription_id,
            )).first() is not None
        ))
        events.append({
            **row.model_dump(exclude={"properties_json", "user_id"}),
            "properties": properties,
            **({"notification_content": notification_copy} if notification_copy is not None else {}),
            **({"journey": journey_context} if journey_context else {}),
            "server_verified_transaction": owned_transaction,
            "server_transaction_product_id": transaction.product_id if owned_transaction else None,
            "server_purchase_environment": getattr(transaction.environment, "value", transaction.environment) if owned_transaction else None,
        })
    return {
        "analytics_environment": analytics_environment,
        "count": len(events), "truncated": truncated,
        "proof_scope": "Client diagnostic delivery only. Flight identity links correlate client-reported facts, not verified flight assignment. Verified revenue comes from Apple JWS; sequence gaps can also reflect delayed delivery or report filters.",
        "event_counts": dict(Counter(row.event_name for row in rows)),
        "flight_journeys": [{
            "installation_id": key[0],
            "search_journey_id": key[1],
            "selected_flight_ids": sorted({json.loads(row.properties_json)["flight_id"] for row in group
                if row.event_name == "flight_selected" and json.loads(row.properties_json).get("flight_id")}),
            "added_flight_ids": sorted({json.loads(row.properties_json)["flight_id"] for row in group
                if row.event_name == "flight_added" and json.loads(row.properties_json).get("flight_id")}),
            "viewed_flight_ids": sorted({json.loads(row.properties_json)["flight_id"] for row in group
                if row.event_name == "screen_flight_detail_viewed" and json.loads(row.properties_json).get("flight_id")}),
            "selected_flight_identities": sorted({properties_by_id[row.id]["flight_identity"] for row in group
                if row.event_name == "flight_selected" and properties_by_id[row.id].get("flight_identity")}),
            # Provider airport results need not have a backend ID at selection.
            # Keep reported IDs above distinct from later client identity joins.
            "correlated_selected_flight_ids": sorted({properties_by_id[linked.id]["flight_id"]
                for row in group if row.event_name == "flight_selected" and properties_by_id[row.id].get("flight_identity")
                for linked in identities[(key[0], properties_by_id[row.id]["flight_identity"])]
                if properties_by_id[linked.id].get("flight_id")}),
            "failure_events": sum(row.event_name in ("flight_add_failed", "flight_add_blocked") for row in group),
        } for key, group in journeys.items()],
        "flight_identity_links": [{
            "installation_id": key[0], "flight_identity": key[1],
            "backend_flight_ids": sorted({properties_by_id[row.id]["flight_id"] for row in group
                if properties_by_id[row.id].get("flight_id")}),
            "event_counts": dict(Counter(row.event_name for row in group)),
            "paywall_presentation_ids": sorted({row.paywall_presentation_id for row in group if row.paywall_presentation_id}),
            "checkout_attempt_ids": sorted({row.checkout_attempt_id for row in group if row.checkout_attempt_id}),
        } for key, group in sorted(identities.items())],
        "checkout_attempts": [{
            "checkout_attempt_id": key,
            "initiated_count": sum(row.event_name == "af_initiated_checkout" for row in group),
            "terminal_count": sum(row.event_name == "checkout_attempt_completed" for row in group),
            "outcomes": [json.loads(row.properties_json).get("outcome") for row in group
                         if row.event_name == "checkout_attempt_completed"],
            "reported_product_ids": sorted({properties_by_id[row.id]["product_id"] for row in group
                if properties_by_id[row.id].get("product_id")}),
            "reported_displayed_product_ids": sorted({properties_by_id[row.id]["displayed_product_id"] for row in group
                if properties_by_id[row.id].get("displayed_product_id")}),
        } for key, group in attempts.items()],
        "paywall_presentations": [{
            "paywall_presentation_id": key,
            "viewed_count": sum(row.event_name == "paywall_viewed" for row in group),
            "default_selection_count": sum(row.event_name == "subscription_product_selected"
                and json.loads(row.properties_json).get("selection_method") == "default" for row in group),
            "reported_product_ids": sorted({product for row in group
                for product in [json.loads(row.properties_json).get("product_id")]
                if product}),
            "reported_displayed_product_ids": sorted({properties_by_id[row.id]["displayed_product_id"] for row in group
                if properties_by_id[row.id].get("displayed_product_id")}),
            "reported_offer_eligibility": sorted({value for row in group
                for value in [json.loads(row.properties_json).get("offer_eligibility")]
                if value}),
            "reported_legacy_fallback": sorted({value for row in group
                for value in [json.loads(row.properties_json).get("used_legacy_fallback")]
                if value is not None}),
        } for key, group in presentations.items()],
        "events": events,
    }


@router.get("/notifications/engagement/report", dependencies=[Depends(check_lambda_auth_token)])
def get_notification_engagement_report(
    analytics_environment: Literal["production", "development", "testflight"] = "production",
    since_ms: int | None = None,
    until_ms: int | None = None,
    limit: int = Query(default=2000, ge=1, le=10000),
    session: Session = Depends(get_session),
):
    now = current_time_ms()
    statement = select(ExperimentDiagnosticEvent).where(
        ExperimentDiagnosticEvent.event_name == "push_opened",
        ExperimentDiagnosticEvent.analytics_environment == analytics_environment,
        ExperimentDiagnosticEvent.occurred_at_ms >= max(since_ms or 0, now - 90 * 86_400_000),
        ExperimentDiagnosticEvent.occurred_at_ms < (until_ms if until_ms is not None else now + 1),
    )
    rows = session.exec(statement.order_by(ExperimentDiagnosticEvent.occurred_at_ms.desc(),
                                         ExperimentDiagnosticEvent.id).limit(limit + 1)).all()
    truncated = len(rows) > limit
    groups = {}
    for row in rows[:limit]:
        p = json.loads(row.properties_json)
        key = (p.get("notification_copy_id"), p.get("_notification_copy_digest"),
               p.get("notification_type", "unknown"), p.get("app_language", "unknown"), p.get("source", "unknown"))
        if key not in groups:
            groups[key] = {"notification_copy_id": key[0], "notification_type": key[2], "language": key[3],
                "source": key[4], "content": open_copy(p.get("_notification_copy_ciphertext")),
                "opens": 0, "installations": set(), "notifications": set(), "open_delay_buckets": Counter()}
        group = groups[key]
        group["opens"] += 1
        group["installations"].add(row.installation_id)
        if p.get("notification_id"): group["notifications"].add(p["notification_id"])
        group["open_delay_buckets"][p.get("notification_open_delay", "unknown")] += 1
    result = []
    for group in groups.values():
        group["unique_installations"] = len(group.pop("installations"))
        group["distinct_notifications_opened"] = len(group.pop("notifications"))
        result.append(group)
    return {"analytics_environment": analytics_environment, "opens": min(len(rows), limit),
            "truncated": truncated, "groups": sorted(result, key=lambda g: -g["opens"]),
            "open_rate": None,
            "proof_scope": "Observed client notification taps only, not delivery or impressions. No sent/delivered denominator; an open rate cannot be inferred. Historical generic opens have unknown copy. Counts are limited to the returned window; check truncated before comparisons."}
