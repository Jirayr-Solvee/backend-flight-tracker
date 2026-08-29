from time import time

from sqlmodel import Field, SQLModel


def current_time_ms() -> int:
    return int(time() * 1_000)


class ExperimentExposure(SQLModel, table=True):
    id: str = Field(primary_key=True, description="Stable experiment exposure ID")
    experiment_id: str = Field(index=True)
    variant: str = Field(index=True)
    eligible: bool = Field(index=True)
    installation_id: str = Field(index=True)
    app_version: str = Field(index=True)
    build_number: str
    analytics_environment: str = Field(index=True)
    user_id: str = Field(index=True)
    source: str
    exposed_at_ms: int
    first_reported_at_ms: int = Field(default_factory=current_time_ms)


class ExperimentConversion(SQLModel, table=True):
    id: str = Field(
        primary_key=True,
        foreign_key="transaction.id",
        description="Verified App Store transaction ID",
    )
    original_transaction_id: str = Field(index=True)
    experiment_id: str = Field(index=True)
    variant: str = Field(index=True)
    eligible: bool = Field(index=True)
    installation_id: str = Field(index=True)
    exposure_id: str = Field(index=True)
    # The cohort version is the version that originally showed the experiment,
    # even if the verified transaction arrives after a later app update.
    app_version: str = Field(index=True)
    build_number: str
    conversion_app_version: str = Field(index=True)
    conversion_build_number: str
    analytics_environment: str = Field(index=True)
    user_id: str = Field(index=True)
    exposed_at_ms: int | None = None
    product_id: str
    purchase_environment: str = Field(index=True)
    starts_trial: bool = Field(index=True)
    trial_duration_days: int | None = None
    purchase_date_ms: int | None = None
    recorded_at_ms: int = Field(default_factory=current_time_ms)


class ExperimentGoalSelection(SQLModel, table=True):
    id: str = Field(
        primary_key=True,
        description="Stable experiment exposure ID; one final goal set per installation",
    )
    experiment_id: str = Field(index=True)
    variant: str = Field(index=True)
    eligible: bool = Field(index=True)
    installation_id: str = Field(index=True)
    exposure_id: str = Field(index=True)
    app_version: str = Field(index=True)
    build_number: str
    analytics_environment: str = Field(index=True)
    user_id: str = Field(index=True)
    selected_goal_keys: str
    selected_at_ms: int
    first_reported_at_ms: int = Field(default_factory=current_time_ms)
    last_reported_at_ms: int = Field(default_factory=current_time_ms)
