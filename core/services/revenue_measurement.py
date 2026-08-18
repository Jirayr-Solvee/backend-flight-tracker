from typing import Any

from sqlmodel import Session, select

from ..models.apple_ads import AppStoreRevenueEvent, current_time_ms
from ..models.transaction import Transaction
from ..models.user import User, UserSubscriptionLink


def enum_value(value: Any) -> str | None:
    raw_value = getattr(value, "value", value)
    return str(raw_value) if raw_value is not None else None


def upsert_verified_revenue_event(
    *,
    session: Session,
    decoded_jws: Any,
    user_id: str,
) -> AppStoreRevenueEvent:
    """Persist only fields from Apple's verified transaction JWS."""

    transaction_id = str(decoded_jws.transactionId)
    event = session.get(AppStoreRevenueEvent, transaction_id)
    if event is None:
        event = AppStoreRevenueEvent(
            id=transaction_id,
            original_transaction_id=str(decoded_jws.originalTransactionId),
            user_id=user_id,
            product_id=str(decoded_jws.productId),
            purchase_date_ms=int(decoded_jws.purchaseDate or 0),
            purchase_environment=enum_value(decoded_jws.environment) or "unknown",
            currency=str(decoded_jws.currency or "UNKNOWN"),
        )

    discount_type = enum_value(getattr(decoded_jws, "offerDiscountType", None))
    event.original_transaction_id = str(decoded_jws.originalTransactionId)
    event.user_id = user_id
    event.product_id = str(decoded_jws.productId)
    event.purchase_date_ms = int(decoded_jws.purchaseDate or 0)
    event.original_purchase_date_ms = getattr(
        decoded_jws, "originalPurchaseDate", None
    )
    event.signed_date_ms = getattr(decoded_jws, "signedDate", None)
    event.expires_date_ms = getattr(decoded_jws, "expiresDate", None)
    event.revoked_date_ms = getattr(decoded_jws, "revocationDate", None)
    event.revocation_percentage = getattr(
        decoded_jws, "revocationPercentage", None
    )
    event.transaction_reason = enum_value(
        getattr(decoded_jws, "transactionReason", None)
    )
    event.purchase_environment = enum_value(decoded_jws.environment) or "unknown"
    event.price_milliunits = int(getattr(decoded_jws, "price", None) or 0)
    event.currency = str(getattr(decoded_jws, "currency", None) or "UNKNOWN")
    event.app_account_token = (
        str(decoded_jws.appAccountToken)
        if getattr(decoded_jws, "appAccountToken", None)
        else user_id
    )
    event.offer_discount_type = discount_type
    event.offer_period = getattr(decoded_jws, "offerPeriod", None)
    event.starts_trial = discount_type == "FREE_TRIAL"
    event.updated_at_ms = current_time_ms()
    session.add(event)
    return event


def backfill_revenue_event_from_transaction(
    *,
    session: Session,
    transaction: Transaction,
    user_id: str,
) -> AppStoreRevenueEvent:
    """Backfill revenue from transactions that were verified before this table existed."""

    event = AppStoreRevenueEvent(
        id=transaction.id,
        original_transaction_id=transaction.subscription_id,
        user_id=user_id,
        product_id=transaction.product_id or "unknown",
        purchase_date_ms=int(transaction.purchase_date or 0),
        original_purchase_date_ms=transaction.original_purchase_date,
        signed_date_ms=transaction.signed_date,
        expires_date_ms=transaction.expires_date,
        revoked_date_ms=transaction.revoked_date,
        transaction_reason=enum_value(transaction.transaction_reason),
        purchase_environment=enum_value(transaction.environment) or "unknown",
        price_milliunits=int(transaction.price or 0),
        currency=str(transaction.currency or "UNKNOWN"),
        app_account_token=transaction.app_account_token,
        # Historic Transaction rows did not store offer fields, so they must not
        # be guessed as trials. New verified JWS records capture this exactly.
        starts_trial=False,
        updated_at_ms=current_time_ms(),
    )
    session.add(event)
    return event


def backfill_verified_revenue_events(session: Session) -> dict[str, int]:
    """Backfill the owned revenue table from previously verified transactions."""

    created = 0
    skipped_without_user = 0
    transactions = session.exec(select(Transaction)).all()
    for transaction in transactions:
        if session.get(AppStoreRevenueEvent, transaction.id):
            continue

        user_id = None
        if transaction.app_account_token:
            token_user = session.get(User, transaction.app_account_token)
            if token_user:
                user_id = token_user.id
        if user_id is None:
            link = session.exec(
                select(UserSubscriptionLink).where(
                    UserSubscriptionLink.subscription_id
                    == transaction.subscription_id
                )
            ).first()
            user_id = link.user_id if link else None
        if user_id is None:
            skipped_without_user += 1
            continue

        backfill_revenue_event_from_transaction(
            session=session,
            transaction=transaction,
            user_id=user_id,
        )
        created += 1

    session.commit()
    return {
        "created": created,
        "already_present": len(transactions) - created - skipped_without_user,
        "skipped_without_user": skipped_without_user,
    }
