from sqlmodel import Field, SQLModel


class PurchaseActivationRecovery(SQLModel, table=True):
    """Durable state for a verified purchase whose selected flight is still saving."""

    __tablename__ = "purchase_activation_recovery"

    transaction_id: str = Field(
        primary_key=True,
        foreign_key="transaction.id",
    )
    original_transaction_id: str = Field(index=True)
    user_id: str = Field(index=True, foreign_key="user.id")
    flight_id: int | None = Field(default=None, index=True, foreign_key="flight.id")
    state: str = Field(default="recovery_pending", index=True)
    failure_reason: str | None = Field(default=None, index=True)
    experiment_variant: str | None = Field(default=None, index=True)
    app_version: str | None = Field(default=None, index=True)
    build_number: str | None = None
    first_pending_at: int = Field(index=True)
    alert_due_at: int = Field(index=True)
    last_reported_at: int
    resolved_at: int | None = Field(default=None, index=True)
    alerted_at: int | None = Field(default=None, index=True)
