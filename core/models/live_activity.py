import time

from sqlmodel import Field, SQLModel


class LiveActivityRegistration(SQLModel, table=True):
    """An ActivityKit update token scoped to one device and tracked flight."""

    __tablename__ = "live_activity_registration"

    activity_id: str = Field(primary_key=True)
    push_token: str = Field(index=True)
    flight_id: int = Field(foreign_key="flight.id", index=True)
    device_id: str = Field(foreign_key="device.id", index=True)
    apns_environment: str = Field(default="production")
    uses_12_hour_time: bool = Field(default=False)
    active: bool = Field(default=True, index=True)
    created_at: int = Field(default_factory=lambda: int(time.time()))
    updated_at: int = Field(default_factory=lambda: int(time.time()))
    last_delivery_at: int | None = None
    last_apns_timestamp: int | None = None
    last_apns_status: str | None = None
    last_apns_reason: str | None = None
    last_content_state_json: str | None = None


class LiveActivityPushToStartRegistration(SQLModel, table=True):
    """A device token that can start this app's Live Activity while closed."""

    __tablename__ = "live_activity_push_to_start_registration"

    device_id: str = Field(foreign_key="device.id", primary_key=True)
    push_token: str = Field(index=True)
    apns_environment: str = Field(default="production")
    uses_12_hour_time: bool = Field(default=False)
    active: bool = Field(default=True, index=True)
    created_at: int = Field(default_factory=lambda: int(time.time()))
    updated_at: int = Field(default_factory=lambda: int(time.time()))
    last_started_flight_id: int | None = Field(default=None, index=True)
    last_start_at: int | None = None
    last_apns_status: str | None = None
    last_apns_reason: str | None = None


class LiveActivityPushToStartDelivery(SQLModel, table=True):
    """Delivery state for one push-to-start token and tracked flight."""

    __tablename__ = "live_activity_push_to_start_delivery"

    device_id: str = Field(foreign_key="device.id", primary_key=True)
    flight_id: int = Field(foreign_key="flight.id", primary_key=True)
    push_token_fingerprint: str = Field(index=True)
    state: str = Field(default="pending", index=True)
    attempt_count: int = Field(default=0)
    created_at: int = Field(default_factory=lambda: int(time.time()))
    updated_at: int = Field(default_factory=lambda: int(time.time()))
    last_attempt_at: int | None = None
    delivered_at: int | None = None
    confirmed_at: int | None = None
    last_apns_status: str | None = None
    last_apns_reason: str | None = None
