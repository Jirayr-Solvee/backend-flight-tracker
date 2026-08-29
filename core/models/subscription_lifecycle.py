from time import time

from sqlmodel import Field, SQLModel


def current_time_ms() -> int:
    return int(time() * 1_000)


class AppStoreSubscriptionLifecycleEvent(SQLModel, table=True):
    """One verified App Store Server Notification V2 lifecycle event."""

    id: str = Field(
        primary_key=True,
        description="Apple notification UUID or deterministic fallback ID",
    )
    notification_uuid: str | None = Field(default=None, index=True)
    notification_type: str = Field(index=True)
    subtype: str | None = Field(default=None, index=True)
    event_kind: str = Field(index=True)

    original_transaction_id: str | None = Field(default=None, index=True)
    transaction_id: str | None = Field(default=None, index=True)
    user_id: str | None = Field(default=None, index=True)
    product_id: str | None = Field(default=None, index=True)
    purchase_environment: str = Field(index=True)

    notification_signed_date_ms: int | None = Field(default=None, index=True)
    transaction_signed_date_ms: int | None = None
    transaction_purchase_date_ms: int | None = None
    transaction_expires_date_ms: int | None = None
    transaction_revoked_date_ms: int | None = Field(default=None, index=True)
    revocation_reason: int | None = None

    renewal_signed_date_ms: int | None = None
    renewal_date_ms: int | None = None
    auto_renew_status: int | None = Field(default=None, index=True)
    expiration_intent: int | None = Field(default=None, index=True)
    is_in_billing_retry_period: bool | None = Field(default=None, index=True)
    grace_period_expires_date_ms: int | None = None
    subscription_status: int | None = Field(default=None, index=True)

    price_milliunits: int | None = None
    currency: str | None = None
    first_received_at_ms: int = Field(default_factory=current_time_ms, index=True)
    last_received_at_ms: int = Field(default_factory=current_time_ms)
