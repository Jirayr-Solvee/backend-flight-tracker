import asyncio
import ssl
import uuid

import certifi
from aioapns import APNs, NotificationRequest, PushType
from sqlmodel import Session, select

from ...config import settings
from ...models.aerodatabox import FlightStatusEnum
from ...models.device import Device
from ...models.flight import Flight, TimestampTypes
from ...models.notification import DeviceInfo, Notification, NotificationBatch
from ...models.user import User, UserFlightLink

ssl_ctx = ssl.create_default_context(cafile=certifi.where())

with open(settings.APN_KEY_PATH, "r") as f:
    key_content = f.read()

apns_client: APNs | None = None


def get_apns_client() -> APNs:
    global apns_client

    if apns_client is None:
        apns_client = APNs(
            key=key_content,
            key_id=settings.KEY_ID,
            team_id=settings.TEAM_ID,
            topic=settings.BUNDLE_ID,
            use_sandbox=False,
            ssl_context=ssl_ctx,
        )

    return apns_client

class ApnService:
    """
    Firebase Cloud Messaging service class that handles push notifications
    """

    @staticmethod
    async def send_silent_push_notification(apn_token: str):
        request = NotificationRequest(
            device_token=apn_token,
            message={
                "aps": {
                    "content-available": 1
                }
            },
            notification_id=str(uuid.uuid4()),
            time_to_live=3600,
            push_type=PushType.BACKGROUND,
        )

        await get_apns_client().send_notification(request)

    @staticmethod
    async def send_single_push_notification(
        notification: Notification, fcm_token: str, badge_count: int
    ):
        """
        Send a single push notification to a single device
        """
        request = NotificationRequest(
            device_token=fcm_token,
            message={
                "aps": {
                    "alert": {
                        "title": notification.title,
                        "body": notification.body,
                    },
                    "badge": badge_count,
                }
            },
            notification_id=str(uuid.uuid4()),
            time_to_live=3600,
            push_type=PushType.ALERT,
        )

        await get_apns_client().send_notification(request)

    @staticmethod
    async def send_multiple_push_notification(notification_batch: NotificationBatch):
        """
        Send a single push notification to multiple devices at once
        """
        tasks = []

        for device in notification_batch.devices:
            request = NotificationRequest(
                device_token=device.token,
                message={
                    "aps": {
                        "alert": {
                            "title": notification_batch.notification.title,
                            "body": notification_batch.notification.body,
                        },
                        "badge": device.badge,
                    }
                },
                notification_id=str(uuid.uuid4()),
                time_to_live=3600,
                push_type=PushType.ALERT,
            )
            tasks.append(get_apns_client().send_notification(request))

        if not tasks:
            return

        await asyncio.gather(*tasks)

    @staticmethod
    def get_devices_payload_for_a_flight(
        flight_id: int, session: Session
    ) -> list[DeviceInfo]:
        """
        Return a list of FCM tokens of all users linked to a specific flight
        """
        stmt = (
            select(User.id, Device.apn_token, User.notification_count)
            .join(User)  # type: ignore
            .join(UserFlightLink)  # type: ignore
            .join(Flight)  # type: ignore
            .where(
                Flight.id == flight_id,
                Device.apn_token.is_not(None), # type: ignore
                Device.apn_token_active == True,
                Device.user_id == User.id,
                UserFlightLink.user_id == User.id,
                UserFlightLink.flight_id == Flight.id,
            )
        )

        result = session.exec(stmt).all()

        if result:
            return [DeviceInfo(token=token, badge=notification_count + 1, user_id=user_id, notification_count=notification_count) for user_id, token, notification_count in result]  # type: ignore

        return []

    @staticmethod
    def create_status_change_notification(
        status: FlightStatusEnum, flight_full_number: str
    ) -> Notification:
        """
        Return Notification object with proper title and body for status changes
        """
        title = f"Flight {flight_full_number} status updated"

        status_messages = {
            FlightStatusEnum.UNKNOWN: "Your flight status is now unknown.",
            FlightStatusEnum.EXPECTED: "Your flight is expected.",
            FlightStatusEnum.ENROUTE: "Your flight is currently en route.",
            FlightStatusEnum.CHECKIN: "Check-in has started for your flight.",
            FlightStatusEnum.BOARDING: "Boarding has started for your flight.",
            FlightStatusEnum.GATECLOSED: "The gate has closed for your flight.",
            FlightStatusEnum.DEPARTED: "Your flight has departed.",
            FlightStatusEnum.DELAYED: "Your flight is now delayed.",
            FlightStatusEnum.APPROACHING: "Your flight is approaching arrival.",
            FlightStatusEnum.ARRIVED: "Your flight has arrived.",
            FlightStatusEnum.CANCELED: "Your flight has been canceled.",
            FlightStatusEnum.DIVERTED: "Your flight has been diverted.",
            FlightStatusEnum.CANCELEDUNCERTAIN: "Your flight may be canceled.",
        }

        body = status_messages.get(status, "Your flight STATUS have been changed")

        return Notification(title=title, body=body)

    @staticmethod
    def create_status_change_notification_batch(
        status: FlightStatusEnum,
        flight_full_number: str,
        devices_info: list[DeviceInfo],
    ) -> NotificationBatch:
        """
        Return Notification batch object with proper title and body for status changes and a list of fcm tokens
        """
        notification = ApnService.create_status_change_notification(
            status=status, flight_full_number=flight_full_number
        )

        return NotificationBatch(notification=notification, devices=devices_info)

    @staticmethod
    def create_new_flight_added_notification(flight_full_number: str) -> Notification:
        """
        Return Notification object with proper title and body for added flight via forwarded email
        """
        title = "New flight added to your account"
        body = f'Flight "{flight_full_number}" has been added to your account automatically from your forwarded email.'

        return Notification(title=title, body=body)

    @staticmethod
    def create_time_stamp_change_notification(
        location_type: str,
        time_stamp_type: TimestampTypes,
        old_time_stamp: str | None,
        new_time_stamp: str | None,
        flight_number: str,
    ) -> Notification:
        """
        Return Notification object with proper title and body for a new time stamp availability or change
        """
        if not old_time_stamp and new_time_stamp:
            title = f"{location_type} {time_stamp_type.value.lower()} time available"
            body = f"A new {time_stamp_type.value.lower()} time is now available for flight {flight_number}."
        else:
            title = f"{location_type} {time_stamp_type.value.lower()} time updated"
            body = f"The {time_stamp_type.value.lower()} time has changed for flight {flight_number}."

        return Notification(title=title, body=body)

    @staticmethod
    def create_time_stamp_change_notification_batch(
        location_type: str,
        time_stamp_type: TimestampTypes,
        old_time_stamp: str | None,
        new_time_stamp: str | None,
        flight_number: str,
        devices_info: list[DeviceInfo],
    ) -> NotificationBatch:
        """
        Return Batch of Notifications of a new time stamp availability or change
        """
        notification = ApnService.create_time_stamp_change_notification(
            location_type=location_type,
            time_stamp_type=time_stamp_type,
            old_time_stamp=old_time_stamp,
            new_time_stamp=new_time_stamp,
            flight_number=flight_number,
        )

        return NotificationBatch(notification=notification, devices=devices_info)

    @staticmethod
    def create_gate_change_notification(
        location_type: str,
        gate_type: str,
        old_value: str | None,
        new_value: str | None,
        flight_number: str,
    ) -> Notification:
        if not old_value and new_value:
            title = f"New {location_type} {gate_type} available"
            body = f"A new {gate_type.lower()} is now available for flight {flight_number}."
        else:
            title = f"{location_type} {gate_type} changed"
            body = f"The {gate_type.lower()} has changed from {old_value} to {new_value} for flight {flight_number}."

        return Notification(title=title, body=body)

    @staticmethod
    def create_gate_change_notification_batch(
        location_type: str,
        gate_type: str,
        old_value: str | None,
        new_value: str | None,
        flight_number: str,
        devices_info: list[DeviceInfo],
    ) -> NotificationBatch:
        notification = ApnService.create_gate_change_notification(
            location_type=location_type,
            gate_type=gate_type,
            old_value=old_value,
            new_value=new_value,
            flight_number=flight_number,
        )

        return NotificationBatch(notification=notification, devices=devices_info)

    @staticmethod
    def create_aircraft_updated_notification(
        flight_number: str,
        old_reg: str | None,
        new_reg: str | None,
        new_model: str | None,
        old_model: str | None,
    ) -> Notification:
        title = f"Aircraft updated for {flight_number}"

        if not old_reg and new_reg:
            body = f"Aircraft {new_reg} ({new_model or 'Unknown Model'}) has been assigned to your flight."
        elif old_reg != new_reg:
            body = f"Aircraft changed to {new_reg} ({new_model or 'Unknown Model'})."
        elif old_model != new_model:
            body = f"The aircraft model for your flight {new_reg} has been updated to {new_model}."
        else:
            body = f"Aircraft information has been updated for flight {flight_number}."

        return Notification(title=title, body=body)

    @staticmethod
    def create_aircraft_updated_notification_batch(
        flight_number: str,
        old_reg: str | None,
        new_reg: str | None,
        old_model: str | None,
        new_model: str | None,
        devices_info: list[DeviceInfo],
    ):
        notification = ApnService.create_aircraft_updated_notification(
            flight_number=flight_number,
            old_reg=old_reg,
            new_reg=new_reg,
            old_model=old_model,
            new_model=new_model,
        )

        return NotificationBatch(notification=notification, devices=devices_info)
