"""Additive journey ledgers. Never repurpose a legacy experiment/financial PK."""

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from .experiment import current_time_ms


class ActivationJourneyIdentity(SQLModel, table=True):
    """Ownership reservation only, including metadata arriving before enrollment."""
    id: str = Field(primary_key=True)
    user_id: str
    protocol: str = "journey"
    frozen_context_json: str | None = None


class ActivationJourneyAssignment(SQLModel, table=True):
    """Server proposal only; not proof of common pre-welcome enrollment."""
    id: str = Field(primary_key=True)
    installation_id: str = Field(index=True)
    user_id: str = Field(index=True)
    context_json: str
    request_json: str
    created_at_ms: int = Field(default_factory=current_time_ms)


class ActivationJourneyEnrollment(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("enrollment_event_id"),)
    id: str = Field(primary_key=True)
    experiment_id: str = Field(index=True)
    measurement_revision: int
    installation_id: str = Field(index=True)
    user_id: str = Field(index=True)
    variant: str = Field(index=True)
    eligible: bool
    randomized: bool
    app_version: str = Field(index=True)
    build_number: str = Field(index=True)
    analytics_environment: str = Field(index=True)
    enrolled_at_ms: int = Field(index=True)
    enrollment_event_id: str
    assignment_source: str
    config_version: str
    context_json: str
    first_reported_at_ms: int = Field(default_factory=current_time_ms)


class ActivationJourneyDiagnosticContext(SQLModel, table=True):
    id: str = Field(primary_key=True, foreign_key="experimentdiagnosticevent.id")
    enrollment_id: str = Field(index=True)
    user_id: str = Field(index=True)
    context_json: str


class ActivationJourneySelection(SQLModel, table=True):
    """First eligible selected-flight transition, not a replacement denominator."""
    id: str = Field(primary_key=True)
    event_id: str = Field(unique=True)
    user_id: str
    selected_at_ms: int
    flight_identity: str
    flight_id: int | None = None
    first_reported_at_ms: int = Field(default_factory=current_time_ms)


class ActivationJourneyAttribution(SQLModel, table=True):
    """One original subscription can belong to one journey; revenue stays separate."""
    id: str = Field(primary_key=True, description="Original verified Apple transaction ID")
    enrollment_id: str = Field(index=True)
    user_id: str = Field(index=True)
    first_transaction_id: str = Field(foreign_key="transaction.id")
    context_json: str
    attributed_at_ms: int = Field(default_factory=current_time_ms)


class ActivationJourneyGoalSelection(SQLModel, table=True):
    id: str = Field(primary_key=True)
    user_id: str
    confirmation_revision: int
    confirmation_id: str
    selected_at_ms: int
    selected_goal_keys: str
    payload_sha256: str
    reported_at_ms: int = Field(default_factory=current_time_ms)


class ActivationJourneyGoalReceipt(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("enrollment_id", "confirmation_revision"),)
    id: str = Field(primary_key=True)
    enrollment_id: str = Field(index=True)
    user_id: str
    confirmation_revision: int
    payload_sha256: str
