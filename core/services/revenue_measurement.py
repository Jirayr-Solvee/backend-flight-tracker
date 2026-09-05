from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, select

from ..models.apple_ads import AppStoreRevenueEvent, current_time_ms
from ..models.transaction import Transaction
from ..models.subscription_lifecycle import AppStoreSubscriptionLifecycleEvent
from ..models.user import User, UserSubscriptionLink
from ..utils import calculate_premium_valid_until


def enum_value(value: Any) -> str | None:
    raw_value = getattr(value, "value", value)
    return str(raw_value) if raw_value is not None else None


def _freshness_condition(stored_signed, incoming_signed, stored_revoked, incoming_revoked):
    # Equal-date refund enrichment is allowed. Equal/older unrevoked client
    # payloads cannot undo a server-observed refund.
    return or_(and_(stored_signed.is_(None), incoming_signed.is_not(None)),
               incoming_signed > stored_signed,
               and_(or_(incoming_signed == stored_signed,
                        and_(incoming_signed.is_(None), stored_signed.is_(None))),
                    stored_revoked.is_(None), incoming_revoked.is_not(None)))


def upsert_verified_transaction(*, session: Session, decoded_jws: Any,
                                fallback_user_id: str | None = None) -> tuple[Transaction, bool]:
    """Atomically keep the freshest Apple snapshot even across independent workers."""
    values = {
        "id": str(decoded_jws.transactionId), "subscription_id": str(decoded_jws.originalTransactionId),
        "product_id": decoded_jws.productId, "purchase_date": decoded_jws.purchaseDate,
        "original_purchase_date": decoded_jws.originalPurchaseDate, "signed_date": decoded_jws.signedDate,
        "expires_date": decoded_jws.expiresDate, "transaction_reason": enum_value(decoded_jws.transactionReason),
        "price": decoded_jws.price, "currency": decoded_jws.currency, "is_upgraded": decoded_jws.isUpgraded,
        "environment": enum_value(decoded_jws.environment), "revoked_date": decoded_jws.revocationDate,
        "app_account_token": str(decoded_jws.appAccountToken) if getattr(decoded_jws, "appAccountToken", None) else fallback_user_id,
    }
    statement = insert(Transaction).values(**values)
    statement = statement.on_conflict_do_update(index_elements=["id"], set_=values,
        where=_freshness_condition(Transaction.signed_date, statement.excluded.signed_date,
                                   Transaction.revoked_date, statement.excluded.revoked_date))
    updated = session.exec(statement.returning(Transaction.id)).scalar_one_or_none() is not None
    transaction = session.get(Transaction, values["id"])
    session.refresh(transaction)
    return transaction, updated


def refresh_current_entitlement(*, session: Session, user: User) -> None:
    """Derive cache from current linked subscriptions, not a replayed transaction."""
    transactions = session.exec(select(Transaction).join(
        UserSubscriptionLink, UserSubscriptionLink.subscription_id == Transaction.subscription_id,
    ).where(UserSubscriptionLink.user_id == user.id).execution_options(populate_existing=True)).all()
    latest = {}
    for transaction in transactions:
        key = (transaction.purchase_date or 0, transaction.signed_date or 0, transaction.id)
        current = latest.get(transaction.subscription_id)
        if current is None or key > (current.purchase_date or 0, current.signed_date or 0, current.id):
            latest[transaction.subscription_id] = transaction
    candidates = []
    for subscription_id, transaction in latest.items():
        if transaction.revoked_date:
            continue
        lifecycle = session.exec(select(AppStoreSubscriptionLifecycleEvent).where(
            AppStoreSubscriptionLifecycleEvent.original_transaction_id == subscription_id,
            # Informational events (for example CONSUMPTION_REQUEST) can carry
            # the same transaction without any renewal state. They must not mask
            # the latest authoritative grace/expiration notification.
            or_(
                AppStoreSubscriptionLifecycleEvent.renewal_signed_date_ms.is_not(None),
                AppStoreSubscriptionLifecycleEvent.event_kind.in_((
                    "renewal", "expiration", "grace_period_started", "grace_period_expired",
                    "billing_failure", "refund", "refund_reversed", "revocation",
                )),
            ),
            # Renewal state and transaction JWS signature freshness are independent.
            # Re-signing this transaction must not erase a valid grace period; a
            # later refund of an older renewal must not hide this renewal's state.
            or_(
                AppStoreSubscriptionLifecycleEvent.transaction_id == transaction.id,
                and_(
                    AppStoreSubscriptionLifecycleEvent.transaction_id.is_(None),
                    AppStoreSubscriptionLifecycleEvent.notification_signed_date_ms >= (transaction.purchase_date or 0),
                ),
            ),
        ).order_by(AppStoreSubscriptionLifecycleEvent.notification_signed_date_ms.desc())).first()
        if lifecycle and lifecycle.event_kind in (
            "expiration", "grace_period_expired", "refund", "revocation", "billing_failure",
        ):
            continue
        expiry = transaction.expires_date
        if lifecycle and lifecycle.grace_period_expires_date_ms:
            expiry = max(expiry or 0, lifecycle.grace_period_expires_date_ms)
        until = calculate_premium_valid_until(expiry)
        if until is not None:
            candidates.append(until)
    user.premium_valid_until = max(candidates) if candidates else None
    session.add(user)


def refunded_milliunits(price_milliunits: int, revocation_percentage: int | None) -> int:
    """Apple JWS revocationPercentage uses milli-percent: 100_000 means 100%."""
    percentage = 100_000 if revocation_percentage is None else min(100_000, max(0, revocation_percentage))
    # Positive integer half-up rounding keeps both reports identical and avoids
    # binary floats; absence means legacy full refund, explicit zero is retained.
    return (max(0, price_milliunits) * percentage + 50_000) // 100_000


def upsert_verified_revenue_event(
    *,
    session: Session,
    decoded_jws: Any,
    user_id: str,
) -> AppStoreRevenueEvent:
    """Persist only fields from Apple's verified transaction JWS."""

    transaction_id = str(decoded_jws.transactionId)
    discount_type = enum_value(getattr(decoded_jws, "offerDiscountType", None))
    values = {
        "id": transaction_id, "original_transaction_id": str(decoded_jws.originalTransactionId),
        "user_id": user_id, "product_id": str(decoded_jws.productId),
        "purchase_date_ms": int(decoded_jws.purchaseDate or 0),
        "original_purchase_date_ms": getattr(decoded_jws, "originalPurchaseDate", None),
        "signed_date_ms": getattr(decoded_jws, "signedDate", None),
        "expires_date_ms": getattr(decoded_jws, "expiresDate", None),
        "revoked_date_ms": getattr(decoded_jws, "revocationDate", None),
        "revocation_percentage": getattr(decoded_jws, "revocationPercentage", None),
        "transaction_reason": enum_value(getattr(decoded_jws, "transactionReason", None)),
        "purchase_environment": enum_value(decoded_jws.environment) or "unknown",
        "price_milliunits": int(getattr(decoded_jws, "price", None) or 0),
        "currency": str(getattr(decoded_jws, "currency", None) or "UNKNOWN"),
        "app_account_token": str(decoded_jws.appAccountToken) if getattr(decoded_jws, "appAccountToken", None) else user_id,
        "offer_discount_type": discount_type, "offer_period": getattr(decoded_jws, "offerPeriod", None),
        "starts_trial": discount_type == "FREE_TRIAL", "updated_at_ms": current_time_ms(),
    }
    statement = insert(AppStoreRevenueEvent).values(**values)
    session.exec(statement.on_conflict_do_update(index_elements=["id"], set_=values,
        where=_freshness_condition(AppStoreRevenueEvent.signed_date_ms, statement.excluded.signed_date_ms,
                                   AppStoreRevenueEvent.revoked_date_ms, statement.excluded.revoked_date_ms)))
    event = session.get(AppStoreRevenueEvent, transaction_id)
    session.refresh(event)
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
