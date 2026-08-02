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
