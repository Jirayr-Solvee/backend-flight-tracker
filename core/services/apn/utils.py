from enum import Enum

from pydantic import BaseModel
from sqlmodel import Session, select, update

from ...models.aerodatabox import (
    AerodataboxOriginAndDestinationInformationWebhook,
    FlightNotificationContractItem)
from ...models.device import Device
from ...models.flight import Arrival, Departure, Flight
from ...models.notification import DeviceInfo, NotificationBatch
from ...models.user import User, UserFlightLink
from ...utils import get_time
from .service import ApnService, NotificationTimestampTypes
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class DirectionType(str, Enum):
    DEPARTURE = "Departure"
    ARRIVAL = "Arrival"



def extract_all_notifications_for_flight(
    flight: Flight,
    webhook_flight: FlightNotificationContractItem,
    devices_info: list[DeviceInfo],
) -> list[NotificationBatch]:
    notification_batches: list[NotificationBatch] = []

    basic_notification_batchs = extract_basic_notifications_for_flight(
        flight=flight, webhook_flight=webhook_flight, devices_info=devices_info
    )
    notification_batches.extend(basic_notification_batchs)

    if flight.departure:
        nested_notifications_batches = extract_nested_notifications_for_flight(
            flight_number=flight.number,
            db_info=flight.departure,
            webhook_data=webhook_flight.departure,
            devices_info=devices_info,
        )
        notification_batches.extend(nested_notifications_batches)

    if flight.arrival:
        nested_notifications_batches = extract_nested_notifications_for_flight(
            flight_number=flight.number,
            db_info=flight.arrival,
            webhook_data=webhook_flight.arrival,
            devices_info=devices_info,
        )
        notification_batches.extend(nested_notifications_batches)

    return notification_batches


def extract_basic_notifications_for_flight(
    flight: Flight,
    webhook_flight: FlightNotificationContractItem,
    devices_info: list[DeviceInfo],
) -> list[NotificationBatch]:
    notification_batches: list[NotificationBatch] = []

    aircraft_fields = {
        "old_reg": flight.aircraft_reg,
        "old_model": flight.aircraft_model,
        "new_reg": webhook_flight.aircraft.reg if webhook_flight.aircraft else None,
        "new_model": webhook_flight.aircraft.model if webhook_flight.aircraft else None,
    }

    if flight.status != webhook_flight.status:
        batch = ApnService.create_status_change_notification_batch(
            status=webhook_flight.status,  # type: ignore
            flight_full_number=flight.number,
            devices_info=devices_info,
        )
        notification_batches.append(batch)

    new_aircraft = webhook_flight.aircraft
    if new_aircraft and (
        flight.aircraft_reg != new_aircraft.reg
        or flight.aircraft_modeS != new_aircraft.modeS
        or flight.aircraft_model != new_aircraft.model
    ):
        batch = ApnService.create_aircraft_updated_notification_batch(
            flight_number=flight.number, devices_info=devices_info, **aircraft_fields
        )
        notification_batches.append(batch)

    return notification_batches


def extract_nested_notifications_for_flight(
    flight_number: str,
    db_info: Departure | Arrival,
    webhook_data: AerodataboxOriginAndDestinationInformationWebhook,
    devices_info: list[DeviceInfo],
) -> list[NotificationBatch]:
    notification_batches: list[NotificationBatch] = []

    direction = (
        DirectionType.DEPARTURE.value
        if isinstance(db_info, Departure)
        else DirectionType.ARRIVAL.value
    )

    gate_keys_map = {
        "terminal": "terminal",
        "checkin_desk": "checkInDesk",
        "gate": "gate",
        "baggage_belt": "baggageBelt",
    }

    for flight_key, webhook_key in gate_keys_map.items():
        old_value = getattr(db_info, flight_key)
        new_value = getattr(webhook_data, webhook_key)

        if old_value != new_value:
            batch = ApnService.create_gate_change_notification_batch(
                location_type=direction,
                gate_type=flight_key.replace("_", " "),
                old_value=old_value,
                new_value=new_value,
                flight_number=flight_number,
                devices_info=devices_info,
            )
            notification_batches.append(batch)

    class TimeStampModel(BaseModel):
        type: NotificationTimestampTypes
        old: str | None
        new: str | None

    time_stamps = [
        TimeStampModel(
            type=NotificationTimestampTypes.SCHEDULED,
            old=db_info.scheduled_time_utc,
            new=get_time(webhook_data.scheduledTime, "utc"),
        ),
        TimeStampModel(
            type=NotificationTimestampTypes.ESTIMATED,
            old=db_info.predicted_time_utc,
            new=get_time(webhook_data.predictedTime, "utc"),
        ),
        TimeStampModel(
            type=NotificationTimestampTypes.UPDATED,
            old=db_info.revised_time_utc,
            new=get_time(webhook_data.revisedTime, "utc"),
        ),
        TimeStampModel(
            type=NotificationTimestampTypes.ACTUAL,
            old=db_info.runway_time_utc,
            new=get_time(webhook_data.runwayTime, "utc"),
        ),
    ]
    for t in time_stamps:
        if t.new is not None and t.old != t.new:
            batch = ApnService.create_time_stamp_change_notification_batch(
                location_type=direction,
                time_stamp_type=t.type,
                old_time_stamp=t.old,
                new_time_stamp=t.new,
                flight_number=flight_number,
                devices_info=devices_info,
            )
            notification_batches.append(batch)

    return notification_batches


def increase_notifications_of_users(
    session: Session, user_ids: list[str], by_amount: int = 1
):
    if not user_ids:
        return

    session.exec(
        update(User)
        .where(User.id.in_(user_ids))
        .values(notification_count=User.notification_count + by_amount)
    )

def calculate_difference_in_minutes(old_timestamp: str, new_timestamp: str) -> int | None:
    try:
        format_str = "%Y-%m-%d %H:%MZ"

        t1 = datetime.strptime(old_timestamp, format_str)
        t2 = datetime.strptime(new_timestamp, format_str)

        difference = t2 - t1
        return int(difference.total_seconds() / 60)
    except Exception:
        logger.exception(f"unable to caluclate defference in minutes for old_timestamp={old_timestamp}, new_timestamp={new_timestamp}")
        return None