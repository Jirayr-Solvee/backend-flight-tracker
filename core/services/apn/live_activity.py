import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from aioapns import NotificationRequest, PushType
from sqlmodel import Session, select

from ...config import settings
from ...models import engine
from ...models.flight import Flight, FlightOriginAndDestinationInformation
from ...models.live_activity import LiveActivityRegistration
from .service import get_apns_client

logger = logging.getLogger(__name__)

APPLE_REFERENCE_DATE = datetime(2001, 1, 1, tzinfo=timezone.utc)
TERMINAL_STATUSES = {"Arrived", "Canceled", "Diverted", "CanceledUncertain"}
INVALID_TOKEN_REASONS = {
    "BadDeviceToken",
    "DeviceTokenNotForTopic",
    "Unregistered",
}
TRANSIENT_APNS_STATUSES = {"429", "500", "503"}


def _status_value(flight: Flight) -> str:
    return flight.status.value if hasattr(flight.status, "value") else str(flight.status)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.strip().replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _preferred_utc(
    detail: FlightOriginAndDestinationInformation | None,
) -> datetime | None:
    if detail is None:
        return None
    return _parse_datetime(
        detail.runway_time_utc
        or detail.predicted_time_utc
        or detail.revised_time_utc
        or detail.scheduled_time_utc
    )


def _preferred_local(
    detail: FlightOriginAndDestinationInformation | None,
) -> datetime | None:
    if detail is None:
        return None
    return _parse_datetime(
        detail.runway_time_local
        or detail.predicted_time_local
        or detail.revised_time_local
        or detail.scheduled_time_local
    )


def _time_label(
    detail: FlightOriginAndDestinationInformation | None,
    uses_12_hour_time: bool,
) -> str:
    value = _preferred_local(detail)
    if value is None:
        return "--"
    if uses_12_hour_time:
        return value.strftime("%I:%M %p").lstrip("0")
    return value.strftime("%H:%M")


def _activitykit_date(value: datetime | None) -> float | None:
    """Encode Date with Swift Codable's default seconds-since-2001 strategy."""
    if value is None:
        return None
    return (value.astimezone(timezone.utc) - APPLE_REFERENCE_DATE).total_seconds()


def _delay_minutes(detail: FlightOriginAndDestinationInformation | None) -> int | None:
    if detail is None:
        return None
    scheduled = _parse_datetime(detail.scheduled_time_utc)
    actual = _parse_datetime(
        detail.predicted_time_utc
        or detail.runway_time_utc
        or detail.revised_time_utc
    )
    if scheduled is None or actual is None:
        return None
    return int((actual - scheduled).total_seconds() / 60)


def _status_label(status: str) -> str:
    return {
        "Unknown": "Unknown",
        "Expected": "Expected",
        "EnRoute": "En Route",
        "CheckIn": "Check In",
        "Boarding": "Boarding",
        "GateClosed": "Gate Closed",
        "Departed": "Departed",
        "Delayed": "Delayed",
        "Approaching": "Approaching",
        "Arrived": "Arrived",
        "Canceled": "Canceled",
        "Diverted": "Diverted",
        "CanceledUncertain": "Canceled",
    }.get(status, "Unknown")


def _gate_label(flight: Flight, status: str) -> str | None:
    if status in {
        "Unknown",
        "Expected",
        "Delayed",
        "Canceled",
        "Diverted",
        "CanceledUncertain",
    }:
        detail = flight.departure
    else:
        detail = flight.arrival

    if detail is None:
        return None
    return detail.gate or detail.terminal or detail.checkin_desk or detail.baggage_belt


def make_content_state(
    flight: Flight, *, uses_12_hour_time: bool = False
) -> dict[str, Any]:
    status = _status_value(flight)
    departure_date = _preferred_utc(flight.departure)
    arrival_date = _preferred_utc(flight.arrival)
    departure_delay = _delay_minutes(flight.departure) or 0
    arrival_delay = _delay_minutes(flight.arrival) or 0

    # The widget derives live progress from the two Date values. Keeping the
    # fallback stable prevents identical webhook snapshots from consuming the
    # ActivityKit push budget just because wall-clock time advanced.
    fallback_progress = 1.0 if status == "Arrived" else 0.0

    return {
        "flightId": flight.id,
        "flightNumber": flight.number,
        "airlineName": flight.airline.name if flight.airline else None,
        "airlineIata": flight.airline.iata if flight.airline else None,
        "departureIata": (
            flight.departure.airport.iata
            if flight.departure and flight.departure.airport
            else "--"
        ),
        "arrivalIata": (
            flight.arrival.airport.iata
            if flight.arrival and flight.arrival.airport
            else "--"
        ),
        "departureTimeLabel": _time_label(flight.departure, uses_12_hour_time),
        "arrivalTimeLabel": _time_label(flight.arrival, uses_12_hour_time),
        "statusLabel": _status_label(status),
        "gateLabel": _gate_label(flight, status),
        "progress": fallback_progress,
        "departureDate": _activitykit_date(departure_date),
        "arrivalDate": _activitykit_date(arrival_date),
        "isDelayed": max(departure_delay, arrival_delay) > 0 or status == "Delayed",
    }


def make_live_activity_payload(
    flight: Flight,
    *,
    uses_12_hour_time: bool = False,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str, str]:
    now = now or datetime.now(timezone.utc)
    status = _status_value(flight)
    event = "end" if status in TERMINAL_STATUSES else "update"
    content_state = make_content_state(
        flight, uses_12_hour_time=uses_12_hour_time
    )
    content_state_json = json.dumps(
        content_state, sort_keys=True, separators=(",", ":")
    )
    aps: dict[str, Any] = {
        "timestamp": int(now.timestamp()),
        "event": event,
        "content-state": content_state,
    }

    arrival_date = _preferred_utc(flight.arrival)
    if arrival_date is not None and event == "update":
        aps["stale-date"] = int((arrival_date + timedelta(hours=1)).timestamp())
    if event == "end":
        aps["dismissal-date"] = int((now + timedelta(hours=4)).timestamp())

    return {"aps": aps}, event, content_state_json


class LiveActivityService:
    @staticmethod
    async def send_updates_for_flight(flight_id: int) -> None:
        with Session(engine) as session:
            flight = session.get(Flight, flight_id)
            if flight is None:
                logger.warning(
                    "Live Activity delivery skipped: flight_id=%s was not found",
                    flight_id,
                )
                return

            registrations = list(
                session.exec(
                    select(LiveActivityRegistration).where(
                        LiveActivityRegistration.flight_id == flight_id,
                        LiveActivityRegistration.active == True,  # noqa: E712
                    )
                ).all()
            )
            if not registrations:
                return

            deliveries: list[
                tuple[
                    LiveActivityRegistration,
                    NotificationRequest,
                    str,
                    str,
                    bool,
                    int,
                ]
            ] = []
            for registration in registrations:
                # ActivityKit discards updates whose timestamps are older than
                # the last accepted update. Multiple webhook snapshots can be
                # processed within one wall-clock second, so make this value
                # monotonically increasing per activity.
                payload_timestamp = max(
                    int(time.time()),
                    (registration.last_apns_timestamp or 0) + 1,
                )
                payload, event, content_state_json = make_live_activity_payload(
                    flight,
                    uses_12_hour_time=registration.uses_12_hour_time,
                    now=datetime.fromtimestamp(payload_timestamp, tz=timezone.utc),
                )
                if (
                    event == "update"
                    and registration.last_content_state_json == content_state_json
                ):
                    continue

                use_sandbox = registration.apns_environment == "sandbox"
                request = NotificationRequest(
                    device_token=registration.push_token,
                    message=payload,
                    notification_id=str(uuid.uuid4()),
                    time_to_live=14_400 if event == "end" else 3_600,
                    priority=10,
                    collapse_key=f"flight-{flight_id}",
                    push_type=PushType.LIVEACTIVITY,
                    apns_topic=f"{settings.BUNDLE_ID}.push-type.liveactivity",
                )
                deliveries.append(
                    (
                        registration,
                        request,
                        event,
                        content_state_json,
                        use_sandbox,
                        payload_timestamp,
                    )
                )

            if not deliveries:
                return

            async def send_one(request: NotificationRequest, use_sandbox: bool):
                result = None
                for attempt in range(1, 4):
                    try:
                        result = await get_apns_client(
                            use_sandbox=use_sandbox
                        ).send_notification(request)
                    except Exception:
                        if attempt == 3:
                            raise
                        await asyncio.sleep(float(attempt))
                        continue

                    if result.is_successful or str(result.status) not in TRANSIENT_APNS_STATUSES:
                        return result
                    if attempt < 3:
                        await asyncio.sleep(float(attempt))
                return result

            results = await asyncio.gather(
                *[
                    send_one(request, use_sandbox)
                    for _, request, _, _, use_sandbox, _ in deliveries
                ],
                return_exceptions=True,
            )

            delivered_at = int(time.time())
            for delivery, result in zip(deliveries, results):
                (
                    registration,
                    request,
                    event,
                    content_state_json,
                    _,
                    payload_timestamp,
                ) = delivery
                registration.last_delivery_at = delivered_at
                registration.updated_at = delivered_at

                if isinstance(result, BaseException):
                    registration.last_apns_status = "exception"
                    registration.last_apns_reason = type(result).__name__
                    logger.error(
                        "Live Activity APNs delivery raised: activity_id=%s flight_id=%s event=%s",
                        registration.activity_id,
                        flight_id,
                        event,
                    )
                    session.add(registration)
                    continue

                if result is None:
                    registration.last_apns_status = "no_response"
                    registration.last_apns_reason = "No APNs result"
                    session.add(registration)
                    continue

                registration.last_apns_status = str(result.status)
                registration.last_apns_reason = str(result.description)
                if result.is_successful:
                    registration.last_apns_timestamp = payload_timestamp
                    registration.last_content_state_json = content_state_json
                    if event == "end":
                        registration.active = False
                    logger.info(
                        "Live Activity APNs delivered: activity_id=%s flight_id=%s event=%s apns_id=%s",
                        registration.activity_id,
                        flight_id,
                        event,
                        request.notification_id,
                    )
                else:
                    if str(result.description) in INVALID_TOKEN_REASONS:
                        registration.active = False
                    logger.warning(
                        "Live Activity APNs rejected: activity_id=%s flight_id=%s event=%s status=%s reason=%s apns_id=%s",
                        registration.activity_id,
                        flight_id,
                        event,
                        result.status,
                        result.description,
                        request.notification_id,
                    )
                session.add(registration)

            session.commit()
