"""Explicit per-device capability; unknown/old clients remain ineligible."""
from sqlmodel import SQLModel, Field


class StoryPushDevice(SQLModel, table=True):
    device_id: str = Field(primary_key=True, foreign_key="device.id")
    user_id: str = Field(index=True)
    app_version: str
    build_number: int
    capability: int = 0
    enabled: bool = False
    language: str = "en"
    time_zone: str = "UTC"
    environment: str = "unknown"
    updated_at: int


class StoryPushDelivery(SQLModel, table=True):
    # The reservation counts even if APNs times out: never retry an uncertain send.
    id: str = Field(primary_key=True)
    user_id: str = Field(index=True)
    device_id: str
    story_id: str
    story_fingerprint: str
    reserved_at: int = Field(index=True)
    local_day: str
    slot: int
    status: str = "reserved"
