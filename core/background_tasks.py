import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import DefaultDict

import httpx
from sqlmodel import select, update

from .config import settings
from .models import Session, engine
from .models.aerodatabox import (FlightNotificationContractSubscription,
                                 FlightStatusEnum)
from .models.device import Device
from .models.email import S3EmailNotification
from .models.flight import Flight, FlightStatusEnum, UserFlightLink
from .models.user import User
from .services.apn.service import ApnService
from .services.flight import FlightPersistence
from .services.gemini.service import GeminiService
from .utils import get_s3_client, parse_email

logger = logging.getLogger(__name__)


async def handle_incoming_email(notification: S3EmailNotification):
    """
    Background task to handle email parsing form a user for awaiting flights
    """
    with Session(engine) as session:
        try:
            s3_client = get_s3_client()

            obj = s3_client.get_object(Bucket=notification.bucket, Key=notification.key)
            data = obj["Body"].read()

            parsed = parse_email(data)

            user = session.exec(select(User).where(User.email == parsed.sender)).first()
            if not user:
                logger.warning(
                    f"Got an email from unregistered user for following lambda notification payload={notification}"
                )
                return

            # 4. extract flight details
            ai_parser = GeminiService()
            result = await ai_parser.get_function_call(query=parsed.body, email=True)

            if not result:
                logger.warning(
                    "Gemini unable to extract function call for fowrarded email"
                )
                return

            flights = await result.handler(**result.args, session=session)
            if len(flights) > 0:
                # NOTE: edge case: as api may return multiple flights -> we assign the first one -> a sulotion maybe instructing AI model to extract utc of departure and compare it here ( also should have some variable range as many flight trackers have deffrent departure timestamp )

                link = session.exec(
                    select(UserFlightLink).where(
                        UserFlightLink.flight_id == flights[0].id,
                        UserFlightLink.user_id == user.id,
                    )
                ).first()

                if link:
                    logger.info(
                        f"Flight already exsist for flight.id={flights[0].id}, user.id={user.id}, payload={notification}"
                    )
                    # NOTE: maybe send a notification says hey flight already linked to your account
                    return

                FlightPersistence.link_flight_and_user(
                    session=session, flight_id=flights[0].id, user_id=user.id
                )

                push_notification = ApnService.create_new_flight_added_notification(
                    flight_id=flights[0].id,  # type: ignore[arg-type]
                    flight_full_number=flights[0].number
                )

                user.notification_count += 1

                devices: list[Device] = user.devices
                user_devices_tokens = [
                    d.apn_token
                    for d in devices
                    if d.apn_token is not None and d.apn_token_active
                ]
                if user_devices_tokens:
                    for tk in user_devices_tokens:
                        await ApnService.send_single_push_notification(
                            notification=push_notification,
                            fcm_token=tk,
                            badge_count=user.notification_count,
                        )

                session.commit()

        except Exception:
            logger.exception(
                f"Error during flight assignment for a user with following lambda notification payload {notification}"
            )
            session.rollback()


async def create_webhook_for_flight(
    flight_full_number: str,
    flight_id: str,
):
    if settings.DEV_ENV:
        return

    with Session(engine) as session:
        try:
            flight = session.exec(
                select(Flight).where(Flight.number == flight_full_number)
            ).first()

            if not flight:
                logger.warning(
                    f"Unable to find flight number={flight_full_number}, id={flight_id} while creating a webhook"
                )
                return

            similar_flight_with_subscription = session.exec(
                select(Flight).where(
                    Flight.number == flight_full_number,
                    Flight.id != flight_id,
                    Flight.subscription_id != None,
                    Flight.has_subscribed == True,
                )
            ).first()

            if similar_flight_with_subscription:
                flight.has_subscribed = True
                flight.subscription_id = (
                    similar_flight_with_subscription.subscription_id
                )
                session.commit()
                return

            async with httpx.AsyncClient() as client:
                fetcher_url = f"{settings.AERODATABOX_SERVICE_URL}create-webhook?flight_full_number={flight_full_number}"
                response = await client.post(fetcher_url)

                if response.status_code == 200:
                    data = response.json()
                    subscription_id = (
                        FlightNotificationContractSubscription.model_validate(data).id
                    )
                    flight.subscription_id = subscription_id
                    flight.has_subscribed = True
                    session.commit()
                    return

                logger.warning(
                    f"unable to create webhook subscription for flight number={flight_full_number}, fetcher service status_code={response.status_code}"
                )

        except Exception:
            logger.exception(
                f"Error during creationg of webhook subscription for flight number={flight_full_number}"
            )
            session.rollback()


async def delete_webhook(
    subscription_id: str,
):
    try:
        async with httpx.AsyncClient() as client:
            fetcher_url = f"{settings.AERODATABOX_SERVICE_URL}delete-webhook?subscription_id={subscription_id}"
            res = await client.delete(fetcher_url)
            if res.status_code == 200:
                with Session(engine) as session:
                    session.exec(update(Flight).where(Flight.subscription_id == subscription_id).values(subscription_id=None))  # type: ignore
                    session.commit()
                    return
            logger.warning(
                f"response code={res.status_code} returned while removing webhook sub id={subscription_id}"
            )
    except Exception:
        logger.exception(f"Error while removing webhook sub id={subscription_id}")


async def confirm_webhook():
    async with httpx.AsyncClient() as client:
        fetcher_url = f"{settings.AERODATABOX_SERVICE_URL}confirm-webhook-notification"
        await client.put(fetcher_url)


async def remove_hanging_webhooks():
    NOT_ELIGIBLE_STATUS = {
        FlightStatusEnum.UNKNOWN,
        FlightStatusEnum.CANCELED,
        FlightStatusEnum.DIVERTED,
        FlightStatusEnum.CANCELEDUNCERTAIN,
        FlightStatusEnum.ARRIVED,
    }

    def should_delete_webhook(flights: list[Flight]):
        for f in flights:
            if not f.arrival:
                continue

            arrival_utc = (
                f.arrival.runway_time_utc
                or f.arrival.predicted_time_utc
                or f.arrival.revised_time_utc
                or f.arrival.scheduled_time_utc
            )
            if not arrival_utc:
                continue

            dt = datetime.fromisoformat(arrival_utc.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)

            is_older_than_24h = now - dt > timedelta(hours=24)
            if not is_older_than_24h:
                return False

        return True

    while True:
        with Session(engine) as session:
            flights = session.exec(
                select(Flight).where(
                    Flight.subscription_id != None,
                    Flight.has_subscribed == True,
                    Flight.status.notin_(NOT_ELIGIBLE_STATUS),  # type: ignore
                )
            ).all()

            flight_groups: DefaultDict[str, list[Flight]] = defaultdict(list)
            for flight in flights:
                if flight.subscription_id is not None:
                    flight_groups[flight.subscription_id].append(flight)

            for sub_id, flight_group in flight_groups.items():
                if should_delete_webhook(flights=flight_group):
                    await delete_webhook(subscription_id=sub_id)

        await asyncio.sleep(3600)


async def check_and_create_webhook_for_flight():
    NOT_ELIGIBLE_STATUS = {
        FlightStatusEnum.UNKNOWN,
        FlightStatusEnum.CANCELED,
        FlightStatusEnum.DIVERTED,
        FlightStatusEnum.CANCELEDUNCERTAIN,
        FlightStatusEnum.ARRIVED,
    }

    def should_create_webhook(dep_utc: str, arr_utc: str) -> bool:
        dt = datetime.strptime(dep_utc, "%Y-%m-%d %H:%MZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)

        dep_diff = dt - now
        dep_diff = int(dep_diff.total_seconds() // 3600)
        if 0 < dep_diff <= 72:
            return True
        elif dep_diff > 72:
            return False

        at = datetime.strptime(arr_utc, "%Y-%m-%d %H:%MZ").replace(tzinfo=timezone.utc)
        arr_diff = at - now
        arr_diff = int(arr_diff.total_seconds() // 3600)

        if arr_diff > -24:
            return True

        return False

    while True:
        with Session(engine) as session:
            flights = session.exec(
                select(Flight).where(
                    Flight.status.notin_(NOT_ELIGIBLE_STATUS),  # type: ignore
                    Flight.subscription_id == None,
                    Flight.has_subscribed == False,
                )
            ).all()

            flight_groups: DefaultDict[str, list[Flight]] = defaultdict(list)
            for flight in flights:
                flight_groups[flight.number].append(flight)

            # TODO: send them as a btach so no dupliacte db query
            # create_webhook_for_flight should expect list of flight ids
            for flight_number, flight_group in flight_groups.items():
                for f in flight_group:
                    if not f.departure or not f.arrival:
                        continue

                    departure_utc = (
                        f.departure.runway_time_utc
                        or f.departure.predicted_time_utc
                        or f.departure.revised_time_utc
                        or f.departure.scheduled_time_utc
                    )
                    arrival_utc = (
                        f.arrival.runway_time_utc
                        or f.arrival.predicted_time_utc
                        or f.arrival.revised_time_utc
                        or f.arrival.scheduled_time_utc
                    )
                    if not departure_utc or not arrival_utc:
                        continue

                    create_webhook = should_create_webhook(
                        dep_utc=departure_utc, arr_utc=arrival_utc
                    )
                    if create_webhook:
                        await create_webhook_for_flight(flight_full_number=flight_number, flight_id=flight.id)  # type: ignore

        await asyncio.sleep(600)
