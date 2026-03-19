import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..background_tasks import delete_webhook, confirm_webhook
from ..models import get_session
from ..models.aerodatabox import FlightNotificationContract, FlightStatusEnum
from ..models.flight import Departure, Flight
from ..models.notification import NotificationBatch
from ..models.subscription import Subscription
from ..models.transaction import Transaction
from ..services.apn.service import ApnService
from ..services.apn.utils import (extract_all_notifications_for_flight,
                                  increase_notifications_of_users)
from ..services.app_store.service import AppStoreService
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

        global_notification_batchs: list[NotificationBatch] = []

        for f in payload.flights:
            print("1. started")
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
            print(
                f"how many notificaions {len(notification_batchs)}, notification_batchs = {notification_batchs}"
            )
            global_notification_batchs.extend(notification_batchs)

            FlightPersistence.update_flight_from_webhook_data(
                flight=db_flight, webhook_flight=f
            )

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
            for batch in notification_batchs:
                await ApnService.send_multiple_push_notification(
                    notification_batch=batch
                )

        if global_notification_batchs:
            background_tasks.add_task(send_mutiple_batches, global_notification_batchs)

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
    "/app-store-notifications", summary="Receive flight updates from app store"
)
def create_or_update_transaction(
    data: CreateOrUpdateTransactionRequest, session: Session = Depends(get_session)
):
    try:
        notification = AppStoreService.process_notification(
            signed_payload=data.signedPayload
        )
        if (
            not notification
            or not notification.data
            or not notification.data.signedTransactionInfo
        ):
            logger.warning(
                f"Aplpe sent an Invalid notification payload, data={data}, decoded notification={notification}"
            )
            return {"detail": "ok"}

        decoded_jws = AppStoreService.process_transaction(
            signed_jws=notification.data.signedTransactionInfo
        )
        if (
            not decoded_jws
            or not decoded_jws.originalTransactionId
            or not decoded_jws.transactionId
            or not decoded_jws.appAccountToken
        ):
            logger.warning(
                f"Aplpe sent an Invalid JWS payload, decoded_jws={decoded_jws}"
            )
            return {"detail": "ok"}

        # 1. get subsciption if not found create one
        db_subscription = session.get(Subscription, decoded_jws.originalTransactionId)
        if not db_subscription:
            db_subscription = Subscription(
                id=decoded_jws.originalTransactionId,
            )
            session.add(db_subscription)
            session.flush()

        # 2. get transaction if not found create one
        db_transaction = session.get(Transaction, decoded_jws.transactionId)
        if not db_transaction:
            db_transaction = Transaction(
                id=decoded_jws.transactionId, subscription_id=db_subscription.id
            )
        elif (
            db_transaction.signed_date
            and decoded_jws.signedDate
            and db_transaction.signed_date >= decoded_jws.signedDate
        ):
            # skip as this same or old data
            return {"detail": "ok"}

        # 3. update transaction fields
        db_transaction.product_id = decoded_jws.productId
        db_transaction.purchase_date = decoded_jws.purchaseDate
        db_transaction.original_purchase_date = decoded_jws.originalPurchaseDate
        db_transaction.signed_date = decoded_jws.signedDate
        db_transaction.expires_date = decoded_jws.expiresDate
        db_transaction.transaction_reason = decoded_jws.transactionReason
        db_transaction.price = decoded_jws.price
        db_transaction.currency = decoded_jws.currency
        db_transaction.is_upgraded = decoded_jws.isUpgraded
        db_transaction.environment = decoded_jws.environment
        db_transaction.revoked_date = decoded_jws.revocationDate

        premium_until = calculate_premium_valid_until(decoded_jws.expiresDate)

        for user in db_subscription.users:
            user.premium_valid_until = premium_until

        session.add(db_transaction)
        session.commit()
        return {"detail": "ok"}

    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logger.exception(
            f"Unable to cretae/update transaction from apple with follwoing jws payload={data}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
