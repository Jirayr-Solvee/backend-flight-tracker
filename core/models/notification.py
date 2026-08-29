import uuid

from pydantic import BaseModel, Field


class Notification(BaseModel):
    title: str
    body: str
    flight_id: int
    update_type: str
    previous_value: str = ""
    new_value: str = ""
    title_loc_key: str | None = None
    title_loc_args: list[str] = Field(default_factory=list)
    body_loc_key: str | None = None
    body_loc_args: list[str] = Field(default_factory=list)
    priority: int = 0
    notification_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    def apns_custom_payload(self) -> dict[str, int | str]:
        return {
            "flight_id": self.flight_id,
            "update_type": self.update_type,
            "previous_value": self.previous_value,
            "new_value": self.new_value,
            "notification_id": self.notification_id,
        }


class DeviceInfo(BaseModel):
    token: str
    badge: int
    user_id: str
    notification_count: int
    supports_localized_push: bool = False


class NotificationBatch(BaseModel):
    notification: Notification
    devices: list[DeviceInfo] = Field(default_factory=list)
    invoke_review: bool = False
