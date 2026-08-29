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
from enum import Enum
from typing import Tuple

class NotificationTimestampTypes(str, Enum):
    ACTUAL = "Actual"
    ESTIMATED = "Estimated"
    SCHEDULED = "Scheduled"
    UPDATED = "Updated"


ssl_ctx = ssl.create_default_context(cafile=certifi.where())

with open(settings.APN_KEY_PATH, "r") as f:
    key_content = f.read()

apns_clients: dict[bool, APNs] = {}


def get_apns_client(use_sandbox: bool = False) -> APNs:
    client = apns_clients.get(use_sandbox)

    if client is None:
        client = APNs(
            key=key_content,
            key_id=settings.KEY_ID,
            team_id=settings.TEAM_ID,
            topic=settings.BUNDLE_ID,
            use_sandbox=use_sandbox,
            ssl_context=ssl_ctx,
        )
        apns_clients[use_sandbox] = client

    return client

class ApnService:
    """
    Apm service class that handles push notifications
    """

    @staticmethod
    async def send_silent_push_notification(apn_token: str, invoke_review: bool):
        request = NotificationRequest(
            device_token=apn_token,
            message={
                "aps": {
                    "content-available": 1
                },
                "invoke_review": invoke_review,
            },
            notification_id=str(uuid.uuid4()),
            time_to_live=3600,
            push_type=PushType.BACKGROUND,
        )

        await get_apns_client().send_notification(request)

    @staticmethod
    async def send_multiple_silent_push_notification(tokens: list[str], invoke_review: bool):
        tasks = []

        for tk in tokens:
            request = NotificationRequest(
                device_token=tk,
                message={
                    "aps": {
                        "content-available": 1
                    },
                    "invoke_review": invoke_review,
                },
                notification_id=str(uuid.uuid4()),
                time_to_live=3600,
                push_type=PushType.BACKGROUND,
            )
            tasks.append(get_apns_client().send_notification(request))
        if not tasks:
            return

        await asyncio.gather(*tasks)

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
                },
                **notification.apns_custom_payload(),
            },
            notification_id=notification.notification_id,
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
            notification = notification_batch.notification
            alert: dict[str, object] = {
                "title": notification.title,
                "body": notification.body,
            }
            if device.supports_localized_push:
                if notification.title_loc_key:
                    alert["title-loc-key"] = notification.title_loc_key
                    alert["title-loc-args"] = notification.title_loc_args
                    alert.pop("title", None)
                if notification.body_loc_key:
                    alert["loc-key"] = notification.body_loc_key
                    alert["loc-args"] = notification.body_loc_args
                    alert.pop("body", None)

            request = NotificationRequest(
                device_token=device.token,
                message={
                    "aps": {
                        "alert": alert,
                        "badge": device.badge,
                    },
                    **notification.apns_custom_payload(),
                    "request_review": notification_batch.invoke_review,
                },
                notification_id=notification.notification_id,
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
            select(
                User.id,
                Device.apn_token,
                User.notification_count,
                Device.supports_localized_push,
            )
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
            return [
                DeviceInfo(
                    token=token,
                    badge=notification_count + 1,
                    user_id=user_id,
                    notification_count=notification_count,
                    supports_localized_push=supports_localized_push,
                )
                for user_id, token, notification_count, supports_localized_push in result
            ]  # type: ignore

        return []

    @staticmethod
    def create_status_change_notification(
        flight_id: int,
        previous_status: str,
        status: FlightStatusEnum,
        flight_full_number: str,
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
        priority_by_status = {
            FlightStatusEnum.CANCELED: 120,
            FlightStatusEnum.DIVERTED: 120,
            FlightStatusEnum.CANCELEDUNCERTAIN: 115,
            FlightStatusEnum.DELAYED: 110,
            FlightStatusEnum.BOARDING: 105,
            FlightStatusEnum.GATECLOSED: 105,
            FlightStatusEnum.DEPARTED: 100,
            FlightStatusEnum.ARRIVED: 100,
            FlightStatusEnum.CHECKIN: 95,
            FlightStatusEnum.APPROACHING: 90,
            FlightStatusEnum.ENROUTE: 85,
            FlightStatusEnum.EXPECTED: 40,
            FlightStatusEnum.UNKNOWN: 10,
        }

        return Notification(
            title=title,
            body=body,
            flight_id=flight_id,
            update_type="status",
            previous_value=previous_status,
            new_value=status.value,
            title_loc_key="Flight %@ status updated",
            title_loc_args=[flight_full_number],
            body_loc_key=body,
            priority=priority_by_status.get(status, 80),
        )

    @staticmethod
    def create_status_change_notification_batch(
        flight_id: int,
        previous_status: str,
        status: FlightStatusEnum,
        flight_full_number: str,
        devices_info: list[DeviceInfo],
    ) -> NotificationBatch:
        """
        Return Notification batch object with proper title and body for status changes and a list of fcm tokens
        """
        notification = ApnService.create_status_change_notification(
            flight_id=flight_id,
            previous_status=previous_status,
            status=status,
            flight_full_number=flight_full_number,
        )

        batch = NotificationBatch(notification=notification, devices=devices_info)

        if status not in {FlightStatusEnum.UNKNOWN, FlightStatusEnum.EXPECTED}:
            batch.invoke_review = True

        return batch

    @staticmethod
    def create_new_flight_added_notification(
        flight_id: int, flight_full_number: str
    ) -> Notification:
        """
        Return Notification object with proper title and body for added flight via forwarded email
        """
        title = "New flight added to your account"
        body = f'Flight "{flight_full_number}" has been added to your account automatically from your forwarded email.'

        return Notification(
            title=title,
            body=body,
            flight_id=flight_id,
            update_type="flight_added",
            new_value=flight_full_number,
        )

    @staticmethod
    def create_time_stamp_change_notification(
        flight_id: int,
        location_type: str,
        time_stamp_type: NotificationTimestampTypes,
        old_time_stamp: str | None,
        new_time_stamp: str,
        flight_number: str,
    ) -> Tuple[Notification, bool]:
        """
        Return Notification object with proper title and body for a new time stamp availability or change
        """
        if old_time_stamp is None:
            title = f"{location_type} {time_stamp_type.value.lower()} time available"
            body = (
                f"A new {time_stamp_type.value.lower()} {location_type.lower()} time is now available "
                f"for flight {flight_number}."
            )
            return (
                Notification(
                    title=title,
                    body=body,
                    flight_id=flight_id,
                    update_type="time",
                    new_value=new_time_stamp,
                    title_loc_key="Flight %@ schedule updated",
                    title_loc_args=[flight_number],
                    body_loc_key=(
                        "A new departure time is available for flight %@."
                        if location_type == "Departure"
                        else "A new arrival time is available for flight %@."
                    ),
                    body_loc_args=[flight_number],
                    priority=55,
                ),
                False,
            )

        from .utils import calculate_difference_in_minutes

        difference_in_minutes = calculate_difference_in_minutes(old_timestamp=old_time_stamp, new_timestamp=new_time_stamp)
        if difference_in_minutes is None:
            title = f"{location_type} {time_stamp_type.value.lower()} time available"
            body = (
                f"A new {time_stamp_type.value.lower()} {location_type.lower()} time is now available "
                f"for flight {flight_number}."
            )
            return (
                Notification(
                    title=title,
                    body=body,
                    flight_id=flight_id,
                    update_type="time",
                    previous_value=old_time_stamp,
                    new_value=new_time_stamp,
                    title_loc_key="Flight %@ schedule updated",
                    title_loc_args=[flight_number],
                    body_loc_key="Flight schedule information has been updated.",
                    priority=60,
                ),
                True,
            )

        # delay
        if difference_in_minutes > 0:
            title = f"Flight {flight_number} {location_type.lower()} delayed"
            body = (
                f"Your flight {flight_number} {location_type.lower()} is delayed by "
                f"{difference_in_minutes} min ({time_stamp_type.value.lower()})."
            )
            return (
                Notification(
                    title=title,
                    body=body,
                    flight_id=flight_id,
                    update_type="delay",
                    previous_value=old_time_stamp,
                    new_value=new_time_stamp,
                    title_loc_key="Flight %@ schedule updated",
                    title_loc_args=[flight_number],
                    body_loc_key=(
                        "Flight %@ departure is delayed by %@ min."
                        if location_type == "Departure"
                        else "Flight %@ arrival is delayed by %@ min."
                    ),
                    body_loc_args=[flight_number, str(difference_in_minutes)],
                    priority=115,
                ),
                True,
            )

        # early
        elif difference_in_minutes < 0:
            title = f"Flight {flight_number} {location_type.lower()} moved earlier"
            body = (
                f"Your flight {flight_number} {location_type.lower()} is now "
                f"{abs(difference_in_minutes)} min earlier ({time_stamp_type.value.lower()})."
            )
            return (
                Notification(
                    title=title,
                    body=body,
                    flight_id=flight_id,
                    update_type="time",
                    previous_value=old_time_stamp,
                    new_value=new_time_stamp,
                    title_loc_key="Flight %@ schedule updated",
                    title_loc_args=[flight_number],
                    body_loc_key=(
                        "Flight %@ departure moved %@ min earlier."
                        if location_type == "Departure"
                        else "Flight %@ arrival moved %@ min earlier."
                    ),
                    body_loc_args=[flight_number, str(abs(difference_in_minutes))],
                    priority=90,
                ),
                True,
            )

        # on time
        else:
            title = f"Flight {flight_number} {location_type.lower()} on time"
            body = (
                f"Your flight {flight_number} {location_type.lower()} is on time "
                f"({time_stamp_type.value.lower()})."
            )
            return (
                Notification(
                    title=title,
                    body=body,
                    flight_id=flight_id,
                    update_type="time",
                    previous_value=old_time_stamp,
                    new_value=new_time_stamp,
                    title_loc_key="Flight %@ schedule updated",
                    title_loc_args=[flight_number],
                    body_loc_key=(
                        "Flight %@ departure remains on time."
                        if location_type == "Departure"
                        else "Flight %@ arrival remains on time."
                    ),
                    body_loc_args=[flight_number],
                    priority=65,
                ),
                True,
            )
        

    @staticmethod
    def create_time_stamp_change_notification_batch(
        flight_id: int,
        location_type: str,
        time_stamp_type: NotificationTimestampTypes,
        old_time_stamp: str | None,
        new_time_stamp: str,
        flight_number: str,
        devices_info: list[DeviceInfo],
    ) -> NotificationBatch:
        """
        Return Batch of Notifications of a new time stamp availability or change
        """
        notification, invoke_review = ApnService.create_time_stamp_change_notification(
            flight_id=flight_id,
            location_type=location_type,
            time_stamp_type=time_stamp_type,
            old_time_stamp=old_time_stamp,
            new_time_stamp=new_time_stamp,
            flight_number=flight_number,
        )

        batch = NotificationBatch(notification=notification, devices=devices_info) 
        batch.invoke_review = invoke_review
        return batch

    @staticmethod
    def create_gate_change_notification(
        flight_id: int,
        location_type: str,
        gate_type: str,
        old_value: str | None,
        new_value: str | None,
        flight_number: str,
    ) -> Notification:
        if not old_value and new_value:
            title = f"New {location_type} {gate_type} available"
            body = f"{gate_type.capitalize()} {new_value} is now available for flight {flight_number}."
        else:
            title = f"{location_type} {gate_type} changed"
            body = f"The {gate_type.lower()} has changed from {old_value} to {new_value} for flight {flight_number}."

        update_type_by_field = {
            "gate": "gate",
            "terminal": "terminal",
            "baggage belt": "baggage",
            "checkin desk": "check_in",
        }
        loc_field_names = {
            "gate": "Gate",
            "terminal": "Terminal",
            "baggage belt": "Baggage belt",
            "checkin desk": "Check-in desk",
        }
        loc_field = loc_field_names.get(gate_type, "Flight detail")
        if old_value:
            body_loc_key = f"{loc_field} changed from %@ to %@ for flight %@."
            body_loc_args = [old_value, new_value or "", flight_number]
        else:
            body_loc_key = f"{loc_field} %@ is now available for flight %@."
            body_loc_args = [new_value or "", flight_number]

        return Notification(
            title=title,
            body=body,
            flight_id=flight_id,
            update_type=update_type_by_field.get(gate_type, "flight_details"),
            previous_value=old_value or "",
            new_value=new_value or "",
            title_loc_key="Flight %@ details updated",
            title_loc_args=[flight_number],
            body_loc_key=body_loc_key,
            body_loc_args=body_loc_args,
            priority={
                "gate": 110,
                "terminal": 95,
                "checkin desk": 90,
                "baggage belt": 85,
            }.get(gate_type, 75),
        )

    @staticmethod
    def create_gate_change_notification_batch(
        flight_id: int,
        location_type: str,
        gate_type: str,
        old_value: str | None,
        new_value: str | None,
        flight_number: str,
        devices_info: list[DeviceInfo],
    ) -> NotificationBatch:
        notification = ApnService.create_gate_change_notification(
            flight_id=flight_id,
            location_type=location_type,
            gate_type=gate_type,
            old_value=old_value,
            new_value=new_value,
            flight_number=flight_number,
        )

        return NotificationBatch(
            notification=notification,
            devices=devices_info,
            invoke_review=True,
        )

    @staticmethod
    def create_aircraft_updated_notification(
        flight_id: int,
        flight_number: str,
        old_reg: str | None,
        new_reg: str | None,
        new_model: str | None,
        old_model: str | None,
    ) -> Notification:
        title = f"Aircraft updated for {flight_number}"

        if not old_reg and new_reg:
            body = f"Aircraft {new_reg} ({new_model or 'Unknown Model'}) has been assigned to your flight."
            body_loc_key = "Aircraft %@ (%@) has been assigned to your flight."
            body_loc_args = [new_reg, new_model or "Unknown model"]
        elif old_reg != new_reg:
            body = f"Aircraft changed to {new_reg} ({new_model or 'Unknown Model'})."
            body_loc_key = "Aircraft changed to %@ (%@)."
            body_loc_args = [new_reg or "", new_model or "Unknown model"]
        elif old_model != new_model:
            body = f"The aircraft model for your flight {new_reg} has been updated to {new_model}."
            body_loc_key = "Aircraft %@ model updated to %@."
            body_loc_args = [new_reg or "", new_model or "Unknown model"]
        else:
            body = f"Aircraft information has been updated for flight {flight_number}."
            body_loc_key = "Aircraft information has been updated for flight %@."
            body_loc_args = [flight_number]

        previous_value = old_reg or old_model or ""
        new_value = new_reg or new_model or ""

        return Notification(
            title=title,
            body=body,
            flight_id=flight_id,
            update_type="aircraft",
            previous_value=previous_value,
            new_value=new_value,
            title_loc_key="Aircraft updated for %@",
            title_loc_args=[flight_number],
            body_loc_key=body_loc_key,
            body_loc_args=body_loc_args,
            priority=50,
        )

    @staticmethod
    def create_aircraft_updated_notification_batch(
        flight_id: int,
        flight_number: str,
        old_reg: str | None,
        new_reg: str | None,
        old_model: str | None,
        new_model: str | None,
        devices_info: list[DeviceInfo],
    ):
        notification = ApnService.create_aircraft_updated_notification(
            flight_id=flight_id,
            flight_number=flight_number,
            old_reg=old_reg,
            new_reg=new_reg,
            old_model=old_model,
            new_model=new_model,
        )

        return NotificationBatch(
            notification=notification,
            devices=devices_info,
            invoke_review=True,
        )
