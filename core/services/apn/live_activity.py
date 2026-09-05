import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from aioapns import NotificationRequest, PushType
from sqlalchemy import case, func, update
from sqlmodel import Session, select

from ...config import settings
from ...models import engine
from ...models.device import Device
from ...models.flight import Flight, FlightOriginAndDestinationInformation
from ...models.live_activity import (
    LiveActivityPushToStartDelivery,
    LiveActivityPushToStartRegistration,
    LiveActivityRegistration,
)
from ...models.user import UserFlightLink
from .service import get_apns_client

logger = logging.getLogger(__name__)

APPLE_REFERENCE_DATE = datetime(2001, 1, 1, tzinfo=timezone.utc)
TERMINAL_STATUSES = {"Arrived", "Canceled", "Diverted", "CanceledUncertain"}
INVALID_TOKEN_REASONS = {
    "BadDeviceToken",
    "DeviceTokenNotForTopic",
    "ExpiredToken",
    "Unregistered",
}
TRANSIENT_APNS_STATUSES = {"429", "500", "503"}
PUSH_TO_START_WINDOW = timedelta(hours=4)
PUSH_TO_START_ACTIVE_LOOKBACK = timedelta(hours=12)
MAX_PUSH_TO_START_ACTIVITIES_PER_DEVICE = 3
PUSH_TO_START_STATUSES = {
    "Expected",
    "EnRoute",
    "CheckIn",
    "Boarding",
    "GateClosed",
    "Departed",
    "Delayed",
    "Approaching",
}


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


def _operational_detail(flight: Flight, status: str) -> tuple[str | None, str | None]:
    if status in {
        "Unknown",
        "Expected",
        "CheckIn",
        "Boarding",
        "GateClosed",
        "Delayed",
        "Canceled",
        "Diverted",
        "CanceledUncertain",
    }:
        detail = flight.departure
    else:
        detail = flight.arrival

    if detail is None:
        return None, None
    for kind, value in (
        ("gate", detail.gate), ("terminal", detail.terminal),
        ("check_in", detail.checkin_desk), ("baggage", detail.baggage_belt),
    ):
        if value and value.strip() and value.strip().casefold() not in {"--", "—", "unknown", "none", "null", "n/a"}:
            return kind, value.strip()
    return None, None


def _gate_label(flight: Flight, status: str) -> str | None:
    return _operational_detail(flight, status)[1]


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
    operational_kind, operational_value = _operational_detail(flight, status)

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
        "gateLabel": operational_value,
        "operationalDetailKind": operational_kind,
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


def make_push_to_start_payload(
    flight: Flight,
    *,
    uses_12_hour_time: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    content_state = make_content_state(
        flight, uses_12_hour_time=uses_12_hour_time
    )
    aps: dict[str, Any] = {
        "timestamp": int(now.timestamp()),
        "event": "start",
        "attributes-type": "mainWidgetAttributes",
        "attributes": {"flightId": flight.id},
        "content-state": content_state,
        "alert": {
            "title-loc-key": "Live tracking started for %@",
            "title-loc-args": [flight.number],
            "loc-key": (
                "Sofly is monitoring live status, gate, terminal, and delay updates."
            ),
            "sound": "default",
        },
    }
    arrival_date = _preferred_utc(flight.arrival)
    if arrival_date is not None:
        aps["stale-date"] = int((arrival_date + timedelta(hours=1)).timestamp())
    return {"aps": aps}


def _push_to_start_candidates(
    flights: list[Flight], now: datetime
) -> list[Flight]:
    candidates: list[tuple[datetime, Flight]] = []
    for flight in flights:
        status = _status_value(flight)
        if status not in PUSH_TO_START_STATUSES:
            continue

        departure = _preferred_utc(flight.departure)
        arrival = _preferred_utc(flight.arrival)
        if departure is None or arrival is None:
            continue
        if departure > now + PUSH_TO_START_WINDOW:
            continue
        if departure < now - PUSH_TO_START_ACTIVE_LOOKBACK:
            continue
        if arrival < now:
            continue
        candidates.append((departure, flight))

    candidates.sort(key=lambda item: item[0])
    unique: list[Flight] = []
    seen_flight_ids: set[int] = set()
    for _, flight in candidates:
        if flight.id in seen_flight_ids:
            continue
        seen_flight_ids.add(flight.id)
        unique.append(flight)
    return unique


def _push_token_fingerprint(push_token: str) -> str:
    return hashlib.sha256(push_token.encode("ascii")).hexdigest()[:16]


def _reserve_apns_timestamp(registration: LiveActivityRegistration) -> int | None:
    """Reserve ordering across API/fetcher processes without holding a network lock.

    This is the latest reserved timestamp. Invalidate the deduplication snapshot
    until the newest send succeeds: an older in-flight update may reach the device
    even when its completion loses our CAS race against a newer failed send.
    """
    now = int(time.time())
    previous = func.coalesce(LiveActivityRegistration.last_apns_timestamp, 0)
    with Session(engine) as reservation_session:
        timestamp = reservation_session.exec(
            update(LiveActivityRegistration)
            .where(
                LiveActivityRegistration.activity_id == registration.activity_id,
                LiveActivityRegistration.active == True,  # noqa: E712
                LiveActivityRegistration.push_token == registration.push_token,
                LiveActivityRegistration.flight_id == registration.flight_id,
                LiveActivityRegistration.apns_environment == registration.apns_environment,
                LiveActivityRegistration.uses_12_hour_time == registration.uses_12_hour_time,
            )
            .values(
                last_apns_timestamp=case((previous >= now, previous + 1), else_=now),
                last_content_state_json=None,
            )
            .returning(LiveActivityRegistration.last_apns_timestamp)
        ).scalar_one_or_none()
        reservation_session.commit()
        return timestamp


class LiveActivityService:
    @staticmethod
    async def start_due_activities(
        device_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        with Session(engine) as session:
            statement = select(LiveActivityPushToStartRegistration).where(
                LiveActivityPushToStartRegistration.active == True  # noqa: E712
            )
            if device_id is not None:
                statement = statement.where(
                    LiveActivityPushToStartRegistration.device_id == device_id
                )
            registrations = list(session.exec(statement).all())
            if not registrations:
                return

            now = now or datetime.now(timezone.utc)
            for registration in registrations:
                device = session.get(Device, registration.device_id)
                if device is None:
                    registration.active = False
                    registration.updated_at = int(now.timestamp())
                    session.add(registration)
                    continue

                flights = list(
                    session.exec(
                        select(Flight)
                        .join(UserFlightLink)  # type: ignore[arg-type]
                        .where(UserFlightLink.user_id == device.user_id)
                    ).all()
                )
                candidates = _push_to_start_candidates(flights, now)
                token_fingerprint = _push_token_fingerprint(registration.push_token)
                started_or_pending_count = 0

                for flight in candidates:
                    active_activity = session.exec(
                        select(LiveActivityRegistration).where(
                            LiveActivityRegistration.device_id == registration.device_id,
                            LiveActivityRegistration.flight_id == flight.id,
                            LiveActivityRegistration.active == True,  # noqa: E712
                        )
                    ).first()
                    delivery = session.get(
                        LiveActivityPushToStartDelivery,
                        (registration.device_id, flight.id),
                    )

                    if active_activity is not None:
                        started_or_pending_count += 1
                        if delivery is not None and delivery.state != "confirmed":
                            delivery.state = "confirmed"
                            delivery.confirmed_at = int(now.timestamp())
                            delivery.updated_at = int(now.timestamp())
                            session.add(delivery)
                        continue

                    if delivery is None:
                        delivery = LiveActivityPushToStartDelivery(
                            device_id=registration.device_id,
                            flight_id=flight.id,
                            push_token_fingerprint=token_fingerprint,
                        )
                    elif delivery.push_token_fingerprint != token_fingerprint:
                        delivery.push_token_fingerprint = token_fingerprint
                        delivery.state = "pending"
                        delivery.attempt_count = 0
                        delivery.last_attempt_at = None
                        delivery.delivered_at = None
                        delivery.confirmed_at = None
                        delivery.last_apns_status = None
                        delivery.last_apns_reason = None

                    if delivery.state in {"delivered", "confirmed"}:
                        started_or_pending_count += 1
                        continue
                    if delivery.state == "failed_permanent":
                        continue
                    if started_or_pending_count >= MAX_PUSH_TO_START_ACTIVITIES_PER_DEVICE:
                        continue

                    payload = make_push_to_start_payload(
                        flight,
                        uses_12_hour_time=registration.uses_12_hour_time,
                        now=now,
                    )
                    request = NotificationRequest(
                        device_token=registration.push_token,
                        message=payload,
                        notification_id=str(uuid.uuid4()),
                        time_to_live=3_600,
                        priority=10,
                        collapse_key=f"flight-start-{flight.id}",
                        push_type=PushType.LIVEACTIVITY,
                        apns_topic=f"{settings.BUNDLE_ID}.push-type.liveactivity",
                    )

                    result = None
                    try:
                        for attempt in range(1, 4):
                            delivery.attempt_count += 1
                            delivery.last_attempt_at = int(time.time())
                            result = await get_apns_client(
                                use_sandbox=registration.apns_environment == "sandbox"
                            ).send_notification(request)
                            if (
                                result.is_successful
                                or str(result.status) not in TRANSIENT_APNS_STATUSES
                            ):
                                break
                            if attempt < 3:
                                await asyncio.sleep(float(attempt))
                    except Exception as error:
                        delivery.state = "failed_transient"
                        delivery.last_apns_status = "exception"
                        delivery.last_apns_reason = type(error).__name__
                        delivery.updated_at = int(time.time())
                        registration.last_apns_status = "exception"
                        registration.last_apns_reason = type(error).__name__
                        registration.updated_at = int(time.time())
                        session.add(delivery)
                        session.add(registration)
                        logger.exception(
                            "Live Activity push-to-start delivery raised: device_id=%s flight_id=%s",
                            registration.device_id,
                            flight.id,
                        )
                        continue

                    delivery.updated_at = int(time.time())
                    registration.updated_at = delivery.updated_at
                    if result is None:
                        delivery.state = "failed_transient"
                        delivery.last_apns_status = "no_response"
                        delivery.last_apns_reason = "No APNs result"
                    else:
                        apns_status = str(result.status)
                        apns_reason = str(result.description)
                        delivery.last_apns_status = apns_status
                        delivery.last_apns_reason = apns_reason
                        registration.last_apns_status = apns_status
                        registration.last_apns_reason = apns_reason
                        if result.is_successful:
                            delivery.state = "delivered"
                            delivery.delivered_at = int(time.time())
                            started_or_pending_count += 1
                            registration.last_started_flight_id = flight.id
                            registration.last_start_at = delivery.delivered_at
                            logger.info(
                                "Live Activity push-to-start delivered: device_id=%s flight_id=%s apns_id=%s",
                                registration.device_id,
                                flight.id,
                                request.notification_id,
                            )
                        elif apns_reason in INVALID_TOKEN_REASONS:
                            delivery.state = "failed_permanent"
                            registration.active = False
                            logger.warning(
                                "Live Activity push-to-start token rejected: device_id=%s status=%s reason=%s",
                                registration.device_id,
                                result.status,
                                result.description,
                            )
                        elif apns_status in TRANSIENT_APNS_STATUSES:
                            delivery.state = "failed_transient"
                        else:
                            delivery.state = "failed_permanent"

                    session.add(delivery)
                    session.add(registration)
                    if not registration.active:
                        break

            session.commit()

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
                payload, event, content_state_json = make_live_activity_payload(
                    flight,
                    uses_12_hour_time=registration.uses_12_hour_time,
                )
                if (
                    event == "update"
                    and registration.last_content_state_json == content_state_json
                ):
                    continue

                # Reserving only after deduplication preserves the existing
                # success-based suppression, while SQLite serializes timestamp
                # allocation across the API and fetcher workers.
                payload_timestamp = _reserve_apns_timestamp(registration)
                if payload_timestamp is None:
                    continue
                payload["aps"]["timestamp"] = payload_timestamp
                if event == "end":
                    payload["aps"]["dismissal-date"] = payload_timestamp + 14_400

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
                completion: dict[str, Any] = {
                    "last_delivery_at": delivered_at,
                    "updated_at": delivered_at,
                }

                if isinstance(result, BaseException):
                    completion["last_apns_status"] = "exception"
                    completion["last_apns_reason"] = type(result).__name__
                    logger.error(
                        "Live Activity APNs delivery raised: activity_id=%s flight_id=%s event=%s",
                        registration.activity_id,
                        flight_id,
                        event,
                    )
                elif result is None:
                    completion["last_apns_status"] = "no_response"
                    completion["last_apns_reason"] = "No APNs result"
                else:
                    completion["last_apns_status"] = str(result.status)
                    completion["last_apns_reason"] = str(result.description)
                    if result.is_successful:
                        completion["last_content_state_json"] = content_state_json
                        if event == "end":
                            completion["active"] = False
                        logger.info(
                            "Live Activity APNs delivered: activity_id=%s flight_id=%s event=%s apns_id=%s",
                            registration.activity_id, flight_id, event, request.notification_id,
                        )
                    else:
                        if str(result.description) in INVALID_TOKEN_REASONS:
                            completion["active"] = False
                        logger.warning(
                            "Live Activity APNs rejected: activity_id=%s flight_id=%s event=%s status=%s reason=%s apns_id=%s",
                            registration.activity_id, flight_id, event,
                            result.status, result.description, request.notification_id,
                        )
                # An older response must not rewind successful content or
                # deactivate a rotated token after a newer request has started.
                session.exec(update(LiveActivityRegistration).where(
                    LiveActivityRegistration.activity_id == registration.activity_id,
                    LiveActivityRegistration.push_token == registration.push_token,
                    LiveActivityRegistration.flight_id == flight_id,
                    LiveActivityRegistration.last_apns_timestamp == payload_timestamp,
                    LiveActivityRegistration.apns_environment == registration.apns_environment,
                    LiveActivityRegistration.uses_12_hour_time == registration.uses_12_hour_time,
                ).values(**completion).execution_options(synchronize_session=False))

            session.commit()
