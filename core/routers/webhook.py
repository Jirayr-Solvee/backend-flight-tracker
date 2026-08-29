import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..background_tasks import delete_webhook, confirm_webhook
from ..models import get_session
from ..models.aerodatabox import FlightNotificationContract, FlightStatusEnum
from ..models.apple_ads import AppStoreRevenueEvent
from ..models.flight import Departure, Flight
from ..models.notification import NotificationBatch
from ..models.subscription import Subscription
from ..models.transaction import Transaction
from ..models.user import User
from ..services.apn.service import ApnService
from ..services.apn.live_activity import LiveActivityService
from ..services.apn.utils import (extract_all_notifications_for_flight,
                                  increase_notifications_of_users)
from ..services.app_store.service import AppStoreService
from ..services.revenue_measurement import upsert_verified_revenue_event
from ..services.subscription_lifecycle import upsert_subscription_lifecycle_event
from ..utils import calculate_premium_valid_until

router = APIRouter()

logger = logging.getLogger(__name__)

from ..services.flight import FlightPersistence


@router.post("/aerodatabox", summary="Receive flight updates from AeroDataBox")
async def receive_aerodatabox_update(
    payload: FlightNotificationContract,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """
    Handles incoming flight updates from AeroDataBox.
    """
    ELIGIBLE_STATUS = {
        FlightStatusEnum.EXPECTED,
        FlightStatusEnum.DELAYED,
        FlightStatusEnum.ENROUTE,
        FlightStatusEnum.DEPARTED,
        FlightStatusEnum.CHECKIN,
        FlightStatusEnum.GATECLOSED,
        FlightStatusEnum.BOARDING,
        FlightStatusEnum.APPROACHING,
    }

    if not payload.flights or not payload.subscription:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="How would we update our flights if id or flights are messing",
        )

    try:
        flight_full_number: str | None = None
        updated_flight_ids: set[int] = set()

        global_notification_batchs: list[NotificationBatch] = []

        for f in payload.flights:
            if not flight_full_number:
                flight_full_number = f.number.strip().replace(" ", "")

            scheduled_time_utc = (
                f.departure.scheduledTime.utc if f.departure.scheduledTime else None
            )

            db_flight = session.exec(
                select(Flight)
                .join(Departure)
                .where(
                    Flight.subscription_id == payload.subscription.id,
                    Flight.number == flight_full_number,
                    Departure.scheduled_time_utc == scheduled_time_utc,
                    Departure.flight_id == Flight.id,
                )
            ).first()

            if not db_flight or not db_flight.departure or not db_flight.arrival:
                continue

            devices_info = ApnService.get_devices_payload_for_a_flight(session=session, flight_id=db_flight.id)  # type: ignore

            notification_batchs = extract_all_notifications_for_flight(
                flight=db_flight, webhook_flight=f, devices_info=devices_info
            )
            logger.info(f"how many notificaions {len(notification_batchs)}, notification_batchs = {notification_batchs}")
            global_notification_batchs.extend(notification_batchs)

            FlightPersistence.update_flight_from_webhook_data(
                flight=db_flight, webhook_flight=f
            )
            if db_flight.id is not None:
                updated_flight_ids.add(db_flight.id)

            # NOTE: remove when notifications are linked to device
            linked_user_ids = list({d.user_id for d in devices_info})
            if notification_batchs and linked_user_ids:
                increase_notifications_of_users(
                    session=session, user_ids=linked_user_ids
                )

        if flight_full_number:
            flights = session.exec(
                select(Flight).where(
                    Flight.number == flight_full_number,
                    Flight.subscription_id == payload.subscription.id,
                )
            ).all()

            if not any(f.status in ELIGIBLE_STATUS for f in flights):
                background_tasks.add_task(delete_webhook, payload.subscription.id)

        session.commit()

        async def send_mutiple_batches(notification_batchs: list[NotificationBatch]):
            tokens = set()
            review_tokens = set()

            for batch in notification_batchs:
                tks = [dv.token for dv in batch.devices]
                if batch.invoke_review:
                    tokens.update(tks)
                else:
                    review_tokens.update(tks)

                await ApnService.send_multiple_push_notification(
                    notification_batch=batch
                )

            tokens -= review_tokens

            if tokens:
                await ApnService.send_multiple_silent_push_notification(tokens=list(tokens), invoke_review=False)

            if review_tokens:
                await ApnService.send_multiple_silent_push_notification(tokens=list(review_tokens), invoke_review=True)

        if global_notification_batchs:
            background_tasks.add_task(send_mutiple_batches, global_notification_batchs)

        # ActivityKit updates use a separate per-activity APNs token. Schedule
        # them after the database commit so the payload always reflects the
        # persisted status, gate, and revised timestamps, even when no regular
        # notification was generated for this webhook snapshot.
        for updated_flight_id in updated_flight_ids:
            background_tasks.add_task(
                LiveActivityService.send_updates_for_flight,
                updated_flight_id,
            )

        background_tasks.add_task(confirm_webhook)
        return {"detail": "ok"}
    except Exception:
        logger.exception(f"Unable to update flights with following payload={payload}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Somthing went wrong.",
        )


class CreateOrUpdateTransactionRequest(BaseModel):
    signedPayload: str


@router.post(
    "/app-store-notifications",
    summary="Receive verified App Store Server Notifications",
)
def create_or_update_transaction(
    data: CreateOrUpdateTransactionRequest, session: Session = Depends(get_session)
):
    notification_uuid: str | None = None
    try:
        notification = AppStoreService.process_notification(
            signed_payload=data.signedPayload
        )
        if not notification or not notification.data:
            logger.warning("Apple sent an invalid App Store notification")
            return {"detail": "ok"}

        notification_uuid = getattr(notification, "notificationUUID", None)
        signed_transaction_info = notification.data.signedTransactionInfo
        signed_renewal_info = notification.data.signedRenewalInfo
        decoded_jws = (
            AppStoreService.process_transaction(signed_jws=signed_transaction_info)
            if signed_transaction_info
            else None
        )
        decoded_renewal_info = (
            AppStoreService.process_renewal_info(
                signed_renewal_info=signed_renewal_info
            )
            if signed_renewal_info
            else None
        )
        if not decoded_jws and not decoded_renewal_info:
            logger.warning(
                "Apple notification has no verified transaction or renewal info "
                "notification_uuid=%s",
                notification_uuid,
            )
            return {"detail": "ok"}

        original_transaction_id = (
            getattr(decoded_jws, "originalTransactionId", None)
            or getattr(decoded_renewal_info, "originalTransactionId", None)
        )
        app_account_token = (
            getattr(decoded_jws, "appAccountToken", None)
            or getattr(decoded_renewal_info, "appAccountToken", None)
        )
        db_subscription = (
            session.get(Subscription, str(original_transaction_id))
            if original_transaction_id
            else None
        )
        if original_transaction_id and not db_subscription:
            db_subscription = Subscription(
                id=str(original_transaction_id),
            )
            session.add(db_subscription)
            session.flush()

        transaction_user = (
            session.get(User, str(app_account_token)) if app_account_token else None
        )
        if (
            transaction_user
            and db_subscription
            and db_subscription not in transaction_user.subscriptions
        ):
            transaction_user.subscriptions.append(db_subscription)

        measurement_user_id = (
            transaction_user.id
            if transaction_user
            else (
                db_subscription.users[0].id
                if db_subscription and db_subscription.users
                else (str(app_account_token) if app_account_token else None)
            )
        )

        # Persist the verified notification before transaction freshness checks.
        # Auto-renew and billing notifications commonly repeat an existing
        # transaction JWS, but each notification UUID is a distinct lifecycle
        # fact that must not be dropped.
        upsert_subscription_lifecycle_event(
            session=session,
            notification=notification,
            decoded_transaction=decoded_jws,
            decoded_renewal_info=decoded_renewal_info,
            user_id=measurement_user_id,
        )

        if (
            decoded_jws
            and decoded_jws.originalTransactionId
            and decoded_jws.transactionId
            and db_subscription
        ):
            db_transaction = session.get(Transaction, str(decoded_jws.transactionId))
            is_new_transaction = db_transaction is None
            if db_transaction is None:
                db_transaction = Transaction(
                    id=str(decoded_jws.transactionId),
                    subscription_id=db_subscription.id,
                )
            should_update_transaction = (
                is_new_transaction
                or not db_transaction.signed_date
                or not decoded_jws.signedDate
                or db_transaction.signed_date < decoded_jws.signedDate
            )

            if should_update_transaction:
                db_transaction.product_id = decoded_jws.productId
                db_transaction.purchase_date = decoded_jws.purchaseDate
                db_transaction.original_purchase_date = (
                    decoded_jws.originalPurchaseDate
                )
                db_transaction.signed_date = decoded_jws.signedDate
                db_transaction.expires_date = decoded_jws.expiresDate
                db_transaction.transaction_reason = decoded_jws.transactionReason
                db_transaction.price = decoded_jws.price
                db_transaction.currency = decoded_jws.currency
                db_transaction.is_upgraded = decoded_jws.isUpgraded
                db_transaction.environment = decoded_jws.environment
                db_transaction.revoked_date = decoded_jws.revocationDate
                db_transaction.app_account_token = (
                    str(decoded_jws.appAccountToken)
                    if decoded_jws.appAccountToken
                    else None
                )
                session.add(db_transaction)

            if measurement_user_id and (
                should_update_transaction
                or not session.get(
                    AppStoreRevenueEvent, str(decoded_jws.transactionId)
                )
            ):
                upsert_verified_revenue_event(
                    session=session,
                    decoded_jws=decoded_jws,
                    user_id=measurement_user_id,
                )

            notification_type_value = getattr(notification, "notificationType", None)
            notification_type = (
                getattr(notification_type_value, "value", notification_type_value)
                or getattr(notification, "rawNotificationType", None)
            )
            subtype_value = getattr(notification, "subtype", None)
            notification_subtype = (
                getattr(subtype_value, "value", subtype_value)
                or getattr(notification, "rawSubtype", None)
            )
            should_update_premium = should_update_transaction
            premium_until = None
            if notification_type in {
                "REFUND",
                "REVOKE",
                "EXPIRED",
                "GRACE_PERIOD_EXPIRED",
            } or (
                notification_type == "DID_FAIL_TO_RENEW"
                and notification_subtype != "GRACE_PERIOD"
            ):
                should_update_premium = True
            elif (
                notification_type == "DID_FAIL_TO_RENEW"
                and notification_subtype == "GRACE_PERIOD"
                and decoded_renewal_info
                and decoded_renewal_info.gracePeriodExpiresDate
            ):
                should_update_premium = True
                premium_until = calculate_premium_valid_until(
                    decoded_renewal_info.gracePeriodExpiresDate
                )
            elif not decoded_jws.revocationDate:
                premium_until = calculate_premium_valid_until(
                    decoded_jws.expiresDate
                )

            if should_update_premium:
                for user in db_subscription.users:
                    user.premium_valid_until = premium_until

        session.commit()
        return {"detail": "ok"}

    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logger.exception(
            "Unable to create/update transaction from Apple notification_uuid=%s",
            notification_uuid,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
