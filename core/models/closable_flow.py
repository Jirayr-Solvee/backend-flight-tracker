"""Derived closable-flow funnel ledger; outlives the 90-day diagnostics purge."""

from sqlmodel import Field, SQLModel

from .experiment import current_time_ms


class ClosableFlowFunnelFact(SQLModel, table=True):
    """One derived row per closable-flow enrollment. Never purged.

    Counts and timestamps only: no query text, no free text and no new
    identifiers. The row is recomputed from retained diagnostics until it is
    final (horizon plus settling time), and never replaced by a derivation that
    saw fewer source events.
    """
    id: str = Field(primary_key=True, description="Journey enrollment ID")
    experiment_id: str = Field(index=True)
    measurement_revision: int = Field(index=True)
    variant: str = Field(index=True)
    analytics_environment: str = Field(index=True)
    assignment_source: str
    randomized: bool
    enrolled_at_ms: int = Field(index=True)
    horizon_days: int
    derived_through_ms: int
    final: bool = Field(default=False, index=True)
    retention_complete: bool = True
    source_event_count: int = 0

    first_paywall_at_ms: int | None = None
    paywall_presentations: int = 0
    presentations_by_source_json: str = "{}"
    closable_presentations: int = 0
    lifetime_offer_presentations: int = 0
    forced_control_presentations: int = 0
    lifetime_disabled_presentations: int = 0
    close_presentations: int = 0
    first_close_at_ms: int | None = None
    return_presentations: int = 0
    first_return_at_ms: int | None = None
    first_return_source: str | None = None

    checkout_attempts: int = 0
    terminal_outcomes_json: str = "{}"
    verified_first_presentation_attempts: int = 0
    verified_return_presentation_attempts: int = 0

    retry_panel_shown: int = 0
    retry_checkout_tapped: int = 0
    retry_panel_closed: int = 0
    retry_verified_attempts: int = 0

    reminder_offer_shown: int = 0
    reminder_opted_in: int = 0
    reminder_declined: int = 0
    reminder_turned_off: int = 0
    reminder_push_opened: int = 0
    reminder_paywall_presentations: int = 0
    reminder_verified_attempts: int = 0

    onboarding_rating_prompts: int = 0
    derived_at_ms: int = Field(default_factory=current_time_ms)
