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

router = APIRouter()
Token = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")]
ProductID = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.-]+$")]
EventName = Literal[
    "paywall_viewed", "paywall_dismissed", "flight_detail_paywall_experiment_exposed",
    "flight_detail_paywall_experiment_enrolled",
    "subscription_product_selected", "af_initiated_checkout", "checkout_attempt_completed",
    "af_start_trial", "af_purchase", "purchase_cancelled", "purchase_pending",
    "purchase_unverified", "purchase_error", "flight_selected", "flight_added",
    "screen_flight_detail_viewed", "post_purchase_flight_activation",
    "notification_permission_result", "tracking_briefing_scheduled",
    "flight_notification_scheduling", "live_activity_started", "live_activity_start_failed",
    "live_activity_push_to_start_registration",
    "live_activity_update_registration",
]


class DiagnosticProperties(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
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
    trial_duration_days: Annotated[int, Field(ge=0, le=365)] | None = None
    event_schema_version: Annotated[int, Field(ge=1, le=1000)] | None = None
    attempt_count: Annotated[int, Field(ge=0, le=1000)] | None = None
    notification_type: Token | None = None
    status: Token | None = None
    activity_kind: Token | None = None


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
    properties: DiagnosticProperties = Field(default_factory=DiagnosticProperties)

    @model_validator(mode="after")
    def validate_context(self):
        if self.build_configuration == "debug" and self.analytics_environment != "development":
            raise ValueError("Debug diagnostics must use development environment")
        if self.experiment and (
            self.experiment.installation_id != self.installation_id
            or self.experiment.analytics_environment != self.analytics_environment
        ):
            raise ValueError("Experiment and event context must match")
        if self.event_name in (
            "paywall_viewed", "paywall_dismissed", "subscription_product_selected",
            "af_initiated_checkout", "checkout_attempt_completed",
        ) and self.paywall_presentation_id is None:
            raise ValueError("Paywall presentation ID is required")
        if self.event_name in ("af_initiated_checkout", "checkout_attempt_completed"):
            if self.checkout_attempt_id is None:
                raise ValueError("Checkout attempt ID is required")
        if self.event_name == "checkout_attempt_completed" and self.properties.outcome not in (
            "verified", "cancelled", "pending", "unverified", "error",
        ):
            raise ValueError("Invalid checkout terminal outcome")
        return self


class DiagnosticBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[DiagnosticEvent] = Field(min_length=1, max_length=25)


def _row(event: DiagnosticEvent, user: User) -> ExperimentDiagnosticEvent:
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
        properties_json=json.dumps(event.properties.model_dump(exclude_none=True), sort_keys=True, separators=(",", ":")),
    )


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
        row = _row(event, user)
        existing = incoming.get(row.id) or session.get(ExperimentDiagnosticEvent, row.id)
        if existing:
            facts = lambda value: value.model_dump(exclude={"received_at_ms"})
            if facts(existing) != facts(row):
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
    for row in incoming.values():
        session.add(row)
    try:
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
            if not existing or existing.model_dump(exclude={"received_at_ms"}) != row.model_dump(exclude={"received_at_ms"}):
                raise HTTPException(status_code=409, detail="Concurrent diagnostic facts conflict")
        return {"detail": "success", "accepted": 0, "duplicates": len(data.events)}
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
    events = []
    for row in rows:
        properties = json.loads(row.properties_json)
        if row.checkout_attempt_id:
            attempts[row.checkout_attempt_id].append(row)
        if row.paywall_presentation_id:
            presentations[row.paywall_presentation_id].append(row)
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
            "server_verified_transaction": owned_transaction,
            "server_transaction_product_id": transaction.product_id if owned_transaction else None,
            "server_purchase_environment": getattr(transaction.environment, "value", transaction.environment) if owned_transaction else None,
        })
    return {
        "analytics_environment": analytics_environment,
        "count": len(events), "truncated": truncated,
        "proof_scope": "Client diagnostic delivery only. Verified revenue comes from Apple JWS; sequence gaps can also reflect delayed delivery or report filters.",
        "event_counts": dict(Counter(row.event_name for row in rows)),
        "checkout_attempts": [{
            "checkout_attempt_id": key,
            "initiated_count": sum(row.event_name == "af_initiated_checkout" for row in group),
            "terminal_count": sum(row.event_name == "checkout_attempt_completed" for row in group),
            "outcomes": [json.loads(row.properties_json).get("outcome") for row in group
                         if row.event_name == "checkout_attempt_completed"],
        } for key, group in attempts.items()],
        "paywall_presentations": [{
            "paywall_presentation_id": key,
            "viewed_count": sum(row.event_name == "paywall_viewed" for row in group),
            "default_selection_count": sum(row.event_name == "subscription_product_selected"
                and json.loads(row.properties_json).get("selection_method") == "default" for row in group),
            "reported_product_ids": sorted({product for row in group
                for product in [json.loads(row.properties_json).get("product_id")]
                if product}),
            "reported_offer_eligibility": sorted({value for row in group
                for value in [json.loads(row.properties_json).get("offer_eligibility")]
                if value}),
            "reported_legacy_fallback": sorted({value for row in group
                for value in [json.loads(row.properties_json).get("used_legacy_fallback")]
                if value is not None}),
        } for key, group in presentations.items()],
        "events": events,
    }
