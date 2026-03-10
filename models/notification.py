from pydantic import BaseModel


class Notification(BaseModel):
    title: str
    body: str


class DeviceInfo(BaseModel):
    token: str
    badge: int
    user_id: str
    notification_count: int


class NotificationBatch(BaseModel):
    notification: Notification
    devices: list[DeviceInfo] = []
