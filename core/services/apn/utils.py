from enum import Enum

from pydantic import BaseModel
from sqlmodel import Session, select, update

from ...models.aerodatabox import (
    AerodataboxOriginAndDestinationInformationWebhook,
    FlightNotificationContractItem,
    FlightStatusEnum)
from ...models.device import Device
from ...models.flight import Arrival, Departure, Flight, FlightTimeNotice
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



# A departure or arrival time alert needs the expected time to move at least
# this far from the time users were last told about. Provider estimates drift
# a minute or two at a time; those drifts add up instead of each alerting.
TIME_ALERT_MINUTES = 10

# Once a flight is in these states its status alert tells the story; a moved
# departure (or arrival) estimate is no longer news.
DEPARTURE_TIME_SETTLED = {
    FlightStatusEnum.DEPARTED, FlightStatusEnum.ENROUTE, FlightStatusEnum.APPROACHING,
    FlightStatusEnum.ARRIVED, FlightStatusEnum.CANCELED, FlightStatusEnum.CANCELEDUNCERTAIN,
    FlightStatusEnum.DIVERTED,
}
ARRIVAL_TIME_SETTLED = {
    FlightStatusEnum.ARRIVED, FlightStatusEnum.CANCELED, FlightStatusEnum.CANCELEDUNCERTAIN,
    FlightStatusEnum.DIVERTED,
}


def extract_all_notifications_for_flight(
    flight: Flight,
    webhook_flight: FlightNotificationContractItem,
    devices_info: list[DeviceInfo],
    notified_times: dict[str, str] | None = None,
) -> list[NotificationBatch]:
    """At most one visible alert for a provider snapshot.

    `notified_times` maps direction to the time users were last told about.
    It is updated in place: a first-seen time becomes the baseline, and a
    time alert advances it only when that alert is the one actually sent."""
    notified_times = {} if notified_times is None else notified_times
    notification_batches: list[NotificationBatch] = []
    time_alerts: dict[str, tuple[NotificationBatch, str, str]] = {}

    basic_notification_batchs = extract_basic_notifications_for_flight(
        flight=flight, webhook_flight=webhook_flight, devices_info=devices_info
    )
    notification_batches.extend(basic_notification_batchs)

    for db_info, webhook_info, settled in (
        (flight.departure, webhook_flight.departure, DEPARTURE_TIME_SETTLED),
        (flight.arrival, webhook_flight.arrival, ARRIVAL_TIME_SETTLED),
    ):
        if not db_info:
            continue
        notification_batches.extend(extract_nested_notifications_for_flight(
            flight_id=flight.id,  # type: ignore[arg-type]
            flight_number=flight.number,
            db_info=db_info,
            webhook_data=webhook_info,
            devices_info=devices_info,
        ))
        direction = direction_of(db_info)
        alert, known, announced = extract_time_alert(
            flight_id=flight.id,  # type: ignore[arg-type]
            flight_number=flight.number,
            db_info=db_info,
            webhook_data=webhook_info,
            devices_info=devices_info,
            notified_time=notified_times.get(direction),
            settled=webhook_flight.status in settled,
        )
        if alert:
            notification_batches.append(alert)
            time_alerts[direction] = (alert, known, announced)  # type: ignore[assignment]
        elif known:
            notified_times[direction] = known

    result = consolidate_notification_batches(notification_batches)
    sent_id = result[0].notification.notification_id if result else None
    for direction, (alert, known, announced) in time_alerts.items():
        # A higher-priority alert won this snapshot: keep what users know so
        # the moved time is still announced on the next update.
        notified_times[direction] = announced if alert.notification.notification_id == sent_id else known
    return result


def direction_of(info: Departure | Arrival) -> str:
    return DirectionType.DEPARTURE.value if isinstance(info, Departure) else DirectionType.ARRIVAL.value


def expected_time_utc(scheduled: str | None, revised: str | None, predicted: str | None) -> str | None:
    """The time the app shows: revised, then estimated, then scheduled."""
    return revised or predicted or scheduled


def extract_time_alert(
    flight_id: int,
    flight_number: str,
    db_info: Departure | Arrival,
    webhook_data: AerodataboxOriginAndDestinationInformationWebhook,
    devices_info: list[DeviceInfo],
    notified_time: str | None,
    settled: bool = False,
) -> tuple[NotificationBatch | None, str | None, str | None]:
    """An alert when the expected time has moved TIME_ALERT_MINUTES or more
    from the time users were last told about. Also returns the time users
    know now and the time they will know if the alert is sent.

    The message gives the delay against the schedule, as the app does, so a
    flight 25 minutes late never reads as "delayed by 1 min"."""
    # Status alerts (departed, landed, canceled, diverted) take over, and the
    # runway time means this part of the trip has already happened.
    if settled or db_info.runway_time_utc or get_time(webhook_data.runwayTime, "utc"):
        return None, notified_time, notified_time

    scheduled = get_time(webhook_data.scheduledTime, "utc")
    revised = get_time(webhook_data.revisedTime, "utc")
    predicted = get_time(webhook_data.predictedTime, "utc")
    expected = expected_time_utc(scheduled, revised, predicted)
    if expected is None:
        return None, notified_time, notified_time

    # Never alerted: the time the app already showed is what users know.
    known = notified_time or expected_time_utc(
        db_info.scheduled_time_utc, db_info.revised_time_utc, db_info.predicted_time_utc
    )
    if known is None:
        return None, expected, expected
    moved = calculate_difference_in_minutes(old_timestamp=known, new_timestamp=expected)
    if moved is None:
        return None, expected, expected
    if abs(moved) < TIME_ALERT_MINUTES:
        return None, known, known

    batch = ApnService.create_time_stamp_change_notification_batch(
        flight_id=flight_id,
        location_type=direction_of(db_info),
        time_stamp_type=(
            NotificationTimestampTypes.UPDATED if revised
            else NotificationTimestampTypes.ESTIMATED if predicted
            else NotificationTimestampTypes.SCHEDULED
        ),
        # Against the schedule the message reads as the flight's delay;
        # without one, as the move from what users were last told.
        old_time_stamp=scheduled or known,
        new_time_stamp=expected,
        flight_number=flight_number,
        devices_info=devices_info,
    )
    return batch, known, expected


def consolidate_notification_batches(
    notification_batches: list[NotificationBatch],
) -> list[NotificationBatch]:
    """Return at most one useful visible alert for a provider snapshot."""
    if not notification_batches:
        return []

    _, primary = max(
        enumerate(notification_batches),
        key=lambda item: (item[1].notification.priority, -item[0]),
    )

    return [
        primary.model_copy(
            update={
                "invoke_review": any(
                    batch.invoke_review for batch in notification_batches
                )
            }
        )
    ]


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
            flight_id=flight.id,  # type: ignore[arg-type]
            previous_status=flight.status,
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
            flight_id=flight.id,  # type: ignore[arg-type]
            flight_number=flight.number,
            devices_info=devices_info,
            **aircraft_fields,
        )
        notification_batches.append(batch)

    return notification_batches


def extract_nested_notifications_for_flight(
    flight_id: int,
    flight_number: str,
    db_info: Departure | Arrival,
    webhook_data: AerodataboxOriginAndDestinationInformationWebhook,
    devices_info: list[DeviceInfo],
) -> list[NotificationBatch]:
    """Gate, terminal, check-in desk and baggage belt changes. Time changes
    are extract_time_alert's job."""
    notification_batches: list[NotificationBatch] = []
    direction = direction_of(db_info)

    gate_keys_map = {
        "terminal": "terminal",
        "checkin_desk": "checkInDesk",
        "gate": "gate",
        "baggage_belt": "baggageBelt",
    }

    for flight_key, webhook_key in gate_keys_map.items():
        old_value = getattr(db_info, flight_key)
        new_value = getattr(webhook_data, webhook_key)

        # Providers sometimes retract provisional fields. Persist that change,
        # but do not tell users that a value changed to the literal "None".
        if old_value != new_value and new_value:
            batch = ApnService.create_gate_change_notification_batch(
                flight_id=flight_id,
                location_type=direction,
                gate_type=flight_key.replace("_", " "),
                old_value=old_value,
                new_value=new_value,
                flight_number=flight_number,
                devices_info=devices_info,
            )
            notification_batches.append(batch)

    return notification_batches


def load_notified_times(session: Session, flight_id: int) -> dict[str, str]:
    rows = session.exec(select(FlightTimeNotice).where(FlightTimeNotice.flight_id == flight_id)).all()
    return {row.direction: row.notified_time_utc for row in rows}


def save_notified_times(session: Session, flight_id: int, notified_times: dict[str, str]) -> None:
    for direction, notified_time in notified_times.items():
        row = session.get(FlightTimeNotice, (flight_id, direction))
        if row is None:
            session.add(FlightTimeNotice(flight_id=flight_id, direction=direction, notified_time_utc=notified_time))
        elif row.notified_time_utc != notified_time:
            row.notified_time_utc = notified_time
            session.add(row)


def increase_notifications_of_users(
    session: Session, user_ids: list[str], by_amount: int = 1
):
    if not user_ids:
        return

    session.exec(
        update(User)
        .where(User.id.in_(user_ids)) # type: ignore
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
