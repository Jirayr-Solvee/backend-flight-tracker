import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select

from ..dependency import get_current_user
from ..models import Session, get_session
from ..models.subscription import Subscription
from ..models.transaction import Transaction
from ..models.user import User
from ..services.app_store.service import AppStoreService
from ..utils import calculate_premium_valid_until

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
def get_all(session: Session = Depends(get_session)):
    subscriptions = session.exec(select(Subscription)).all()
    formatted = [
        {"sub": sub, "users": sub.users, "transactions": sub.transactions}
        for sub in subscriptions
    ]
    return formatted


class CreateTransactionRequest(BaseModel):
    jws_payload: str


@router.post("/")
def create_or_update_transaction(
    data: CreateTransactionRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        decoded_jws = AppStoreService.process_transaction(signed_jws=data.jws_payload)
        if (
            not decoded_jws
            or not decoded_jws.originalTransactionId
            or not decoded_jws.transactionId
        ):
            logger.warning(f"Invalid JWS payload {data}, from user id={user.id}")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Invalid JWS"
            )

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

        # now we need to link it to the actual user it self
        if db_subscription not in user.subscriptions:
            user.subscriptions.append(db_subscription)

        premium_until = calculate_premium_valid_until(decoded_jws.expiresDate)

        user.premium_valid_until = premium_until

        session.add(db_transaction)
        session.commit()
        return {"detail": "successfull"}

    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logger.exception(
            f"Unable to cretae/update transaction for user id={user.id}, jws payload={data.jws_payload}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
