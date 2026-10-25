"""Strict capture-time contract, separate from the legacy paywall experiment."""

from typing import Annotated, Literal, NamedTuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import closable_flow_policy as closable_flow

JOURNEY_ID = "activation_journey_2026_09"
JOURNEY_REVISION = 1
JOURNEY_VARIANTS = ("search_first_standard", "goals_flight_detail", "search_first_flight_detail")
JourneyVariant = Literal["search_first_standard", "goals_flight_detail", "search_first_flight_detail"]
# Add variants rather than changing the meaning of a previously captured arm.
# Tuple order: intended onboarding, intended paywall, original goals status.
JOURNEY_SCOPES = {
    "search_first_standard": ("search_first", "standard", "not_asked"),
    "goals_flight_detail": ("goals", "flight_detail", "required"),
    "search_first_flight_detail": ("search_first", "flight_detail", "not_asked"),
}
CLOSABLE_FLOW_ID = closable_flow.EXPERIMENT_ID


class ExperimentDefinition(NamedTuple):
    # revision -> required config_version; None keeps the journey's free token.
    revisions: dict
    scopes: dict
    fallback: str
    min_schema: int


# Experiments share the immutable context shape; meanings are per experiment.
EXPERIMENTS = {
    JOURNEY_ID: ExperimentDefinition(revisions={JOURNEY_REVISION: None}, scopes=JOURNEY_SCOPES,
                                     fallback="search_first_standard", min_schema=18),
    CLOSABLE_FLOW_ID: ExperimentDefinition(
        revisions={revision: closable_flow.config_version(revision) for revision in closable_flow.REVISIONS},
        scopes=closable_flow.SCOPES, fallback=closable_flow.FALLBACK_VARIANT,
        min_schema=closable_flow.MIN_EVENT_SCHEMA),
}
Environment = Literal["production", "testflight", "development"]
BoundedToken = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")]
Millis = Annotated[int, Field(strict=True, ge=0, le=9_223_372_036_854_775_807)]


class ActivationJourneyContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment_id: Literal["activation_journey_2026_09", "closable_flow_2026_10"]
    # Validated against the experiment's revision registry below.
    measurement_revision: Annotated[int, Field(strict=True, ge=1, le=100)]
    variant: Literal["search_first_standard", "goals_flight_detail", "search_first_flight_detail",
                     "control_hard_paywall", "closable_own_flight", "matched_hard_paywall", "matched_soft_paywall"]
    eligible: Annotated[bool, Field(strict=True)]
    randomized: Annotated[bool, Field(strict=True)]
    installation_id: UUID
    exposure_id: str = Field(min_length=1, max_length=140)
    enrollment_event_id: UUID
    enrolled_at_ms: Millis
    app_version: BoundedToken
    build_number: BoundedToken
    analytics_environment: Environment
    assignment_source: Literal[
        "server_assignment", "server_disabled", "configuration_fallback", "local_debug_preview",
    ]
    config_version: BoundedToken
    intended_onboarding: Literal["search_first", "goals", "own_flight_preview"]
    intended_paywall: Literal["standard", "flight_detail", "hard", "closable_one_screen", "hard_one_screen", "soft_one_screen"]
    goals_status: Literal["not_asked", "required"]

    @model_validator(mode="after")
    def coherent_capture(self):
        definition = EXPERIMENTS[self.experiment_id]
        if self.experiment_id == CLOSABLE_FLOW_ID and self.variant not in closable_flow.variants(self.measurement_revision):
            raise ValueError("Variant does not belong to this measurement revision")
        if self.exposure_id != f"{self.experiment_id}:{self.installation_id}":
            raise ValueError("Journey exposure identity must be canonical")
        if self.measurement_revision not in definition.revisions:
            raise ValueError("Measurement revision is not in the experiment registry")
        expected_config = definition.revisions[self.measurement_revision]
        if expected_config is not None and self.config_version != expected_config:
            raise ValueError("Config version must come from the revision registry")
        if self.variant not in definition.scopes:
            raise ValueError("Variant does not belong to this experiment")
        intended = definition.scopes[self.variant]
        if (self.intended_onboarding, self.intended_paywall, self.goals_status) != intended:
            raise ValueError("Journey variant and intended scope conflict")
        if self.assignment_source == "server_assignment":
            if not self.eligible or not self.randomized:
                raise ValueError("Server assignments must be eligible and randomized")
        elif self.eligible or self.randomized:
            raise ValueError("Fallback and preview traffic is not randomized")
        if self.assignment_source == "local_debug_preview" and self.analytics_environment != "development":
            raise ValueError("Local preview is development only")
        fallback = closable_flow.fallback_variant(self.measurement_revision) if self.experiment_id == CLOSABLE_FLOW_ID else definition.fallback
        if self.assignment_source in ("configuration_fallback", "server_disabled") and self.variant != fallback:
            raise ValueError("Fallback uses the experiment's control experience")
        return self


class ActivationJourneyAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: UUID
    enrollment_event_id: UUID
    enrolled_at_ms: Millis
    app_version: BoundedToken
    build_number: BoundedToken
    analytics_environment: Environment
    is_new_installation: Literal[True]
    supports_matched_paywall: Annotated[bool, Field(strict=True)] | None = None


class ActivationJourneyEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    journey: ActivationJourneyContext


def canonical_context(context: ActivationJourneyContext) -> str:
    return context.model_dump_json(exclude_none=True)
