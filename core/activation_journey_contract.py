"""Strict capture-time contract, separate from the legacy paywall experiment."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
Environment = Literal["production", "testflight", "development"]
BoundedToken = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")]
Millis = Annotated[int, Field(strict=True, ge=0, le=9_223_372_036_854_775_807)]


class ActivationJourneyContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment_id: Literal["activation_journey_2026_09"]
    measurement_revision: Literal[1]
    variant: JourneyVariant
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
    intended_onboarding: Literal["search_first", "goals"]
    intended_paywall: Literal["standard", "flight_detail"]
    goals_status: Literal["not_asked", "required"]

    @model_validator(mode="after")
    def coherent_capture(self):
        if self.exposure_id != f"{JOURNEY_ID}:{self.installation_id}":
            raise ValueError("Journey exposure identity must be canonical")
        intended = JOURNEY_SCOPES[self.variant]
        if (self.intended_onboarding, self.intended_paywall, self.goals_status) != intended:
            raise ValueError("Journey variant and intended scope conflict")
        if self.assignment_source == "server_assignment":
            if not self.eligible or not self.randomized:
                raise ValueError("Server assignments must be eligible and randomized")
        elif self.eligible or self.randomized:
            raise ValueError("Fallback and preview traffic is not randomized")
        if self.assignment_source == "local_debug_preview" and self.analytics_environment != "development":
            raise ValueError("Local preview is development only")
        if self.assignment_source in ("configuration_fallback", "server_disabled") and self.variant != "search_first_standard":
            raise ValueError("Fallback uses the standard search-first experience")
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


class ActivationJourneyEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    journey: ActivationJourneyContext


def canonical_context(context: ActivationJourneyContext) -> str:
    return context.model_dump_json(exclude_none=True)
