import hashlib
import json
from typing import Any

from sqlmodel import Session

from ..models.subscription_lifecycle import (
    AppStoreSubscriptionLifecycleEvent,
    current_time_ms,
)


def enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def string_value(value: Any, raw_value: Any = None) -> str | None:
    resolved = enum_value(value)
    if resolved is None:
        resolved = raw_value
    return str(resolved) if resolved is not None else None


def int_value(value: Any, raw_value: Any = None) -> int | None:
    resolved = enum_value(value)
    if resolved is None:
        resolved = raw_value
    return int(resolved) if resolved is not None else None


def lifecycle_event_kind(notification_type: str, subtype: str | None) -> str:
    if notification_type == "DID_CHANGE_RENEWAL_STATUS":
        if subtype == "AUTO_RENEW_DISABLED":
            return "auto_renew_disabled"
        if subtype == "AUTO_RENEW_ENABLED":
            return "auto_renew_enabled"
        return "auto_renew_status_changed"
    if notification_type == "DID_RENEW":
        return "renewal"
    if notification_type == "EXPIRED":
        return "expiration"
    if notification_type == "DID_FAIL_TO_RENEW":
        return (
            "grace_period_started"
            if subtype == "GRACE_PERIOD"
            else "billing_failure"
        )
    if notification_type == "GRACE_PERIOD_EXPIRED":
        return "grace_period_expired"
    if notification_type == "REFUND":
        return "refund"
    if notification_type == "REFUND_REVERSED":
        return "refund_reversed"
    if notification_type == "REFUND_DECLINED":
        return "refund_declined"
    if notification_type == "REVOKE":
        return "revocation"
    return "other"


def lifecycle_metrics(event: AppStoreSubscriptionLifecycleEvent) -> set[str]:
    """Return non-exclusive funnel metrics represented by one Apple event."""

    metrics: set[str] = set()
    if (
        event.notification_type == "DID_CHANGE_RENEWAL_STATUS"
        and event.subtype == "AUTO_RENEW_DISABLED"
    ):
        metrics.add("auto_renew_disabled")
    if (
        event.notification_type == "DID_CHANGE_RENEWAL_STATUS"
        and event.subtype == "AUTO_RENEW_ENABLED"
    ):
        metrics.add("auto_renew_enabled")
    if event.notification_type == "DID_RENEW":
        metrics.add("renewal")
    if event.notification_type == "EXPIRED":
        metrics.add("expiration")
    if event.notification_type == "DID_FAIL_TO_RENEW":
        metrics.add("billing_failure")
        if event.subtype == "GRACE_PERIOD":
            metrics.add("grace_period_started")
    if event.notification_type == "GRACE_PERIOD_EXPIRED":
        metrics.add("grace_period_expired")
    if event.notification_type == "REFUND":
        metrics.add("refund")
    if event.notification_type == "REFUND_REVERSED":
        metrics.add("refund_reversed")
    if event.notification_type == "REFUND_DECLINED":
        metrics.add("refund_declined")
    return metrics


def _fallback_event_id(
    *,
    notification_type: str,
    subtype: str | None,
    notification_signed_date_ms: int | None,
    original_transaction_id: str | None,
    transaction_id: str | None,
) -> str:
    canonical = json.dumps(
        {
            "notification_type": notification_type,
            "subtype": subtype,
            "notification_signed_date_ms": notification_signed_date_ms,
            "original_transaction_id": original_transaction_id,
            "transaction_id": transaction_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "derived:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def upsert_subscription_lifecycle_event(
    *,
    session: Session,
    notification: Any,
    decoded_transaction: Any | None,
    decoded_renewal_info: Any | None,
    user_id: str | None,
) -> AppStoreSubscriptionLifecycleEvent:
    """Persist verified lifecycle facts without retaining either signed JWS."""

    notification_type = string_value(
        getattr(notification, "notificationType", None),
        getattr(notification, "rawNotificationType", None),
    ) or "UNKNOWN"
    subtype = string_value(
        getattr(notification, "subtype", None),
        getattr(notification, "rawSubtype", None),
    )
    original_transaction_id = string_value(
        getattr(decoded_transaction, "originalTransactionId", None)
    ) or string_value(getattr(decoded_renewal_info, "originalTransactionId", None))
    transaction_id = string_value(
        getattr(decoded_transaction, "transactionId", None)
    )
    notification_signed_date_ms = getattr(notification, "signedDate", None)
    notification_uuid = string_value(
        getattr(notification, "notificationUUID", None)
    )
    event_id = notification_uuid or _fallback_event_id(
        notification_type=notification_type,
        subtype=subtype,
        notification_signed_date_ms=notification_signed_date_ms,
        original_transaction_id=original_transaction_id,
        transaction_id=transaction_id,
    )

    event = session.get(AppStoreSubscriptionLifecycleEvent, event_id)
    received_at_ms = current_time_ms()
    if event is None:
        event = AppStoreSubscriptionLifecycleEvent(
            id=event_id,
            notification_uuid=notification_uuid,
            notification_type=notification_type,
            subtype=subtype,
            event_kind=lifecycle_event_kind(notification_type, subtype),
            purchase_environment="unknown",
            first_received_at_ms=received_at_ms,
            last_received_at_ms=received_at_ms,
        )

    data = getattr(notification, "data", None)
    event.notification_uuid = notification_uuid
    event.notification_type = notification_type
    event.subtype = subtype
    event.event_kind = lifecycle_event_kind(notification_type, subtype)
    event.original_transaction_id = original_transaction_id
    event.transaction_id = transaction_id
    event.user_id = user_id
    event.product_id = string_value(
        getattr(decoded_transaction, "productId", None)
    ) or string_value(getattr(decoded_renewal_info, "productId", None))
    event.purchase_environment = (
        string_value(getattr(decoded_transaction, "environment", None))
        or string_value(getattr(decoded_renewal_info, "environment", None))
        or string_value(
            getattr(data, "environment", None),
            getattr(data, "rawEnvironment", None),
        )
        or "unknown"
    )
    event.notification_signed_date_ms = notification_signed_date_ms
    event.transaction_signed_date_ms = getattr(
        decoded_transaction, "signedDate", None
    )
    event.transaction_purchase_date_ms = getattr(
        decoded_transaction, "purchaseDate", None
    )
    event.transaction_expires_date_ms = getattr(
        decoded_transaction, "expiresDate", None
    )
    event.transaction_revoked_date_ms = getattr(
        decoded_transaction, "revocationDate", None
    )
    event.revocation_reason = int_value(
        getattr(decoded_transaction, "revocationReason", None),
        getattr(decoded_transaction, "rawRevocationReason", None),
    )
    event.renewal_signed_date_ms = getattr(decoded_renewal_info, "signedDate", None)
    event.renewal_date_ms = getattr(decoded_renewal_info, "renewalDate", None)
    event.auto_renew_status = int_value(
        getattr(decoded_renewal_info, "autoRenewStatus", None),
        getattr(decoded_renewal_info, "rawAutoRenewStatus", None),
    )
    event.expiration_intent = int_value(
        getattr(decoded_renewal_info, "expirationIntent", None),
        getattr(decoded_renewal_info, "rawExpirationIntent", None),
    )
    event.is_in_billing_retry_period = getattr(
        decoded_renewal_info, "isInBillingRetryPeriod", None
    )
    event.grace_period_expires_date_ms = getattr(
        decoded_renewal_info, "gracePeriodExpiresDate", None
    )
    event.subscription_status = int_value(
        getattr(data, "status", None), getattr(data, "rawStatus", None)
    )
    event.price_milliunits = (
        getattr(decoded_transaction, "price", None)
        if decoded_transaction is not None
        else getattr(decoded_renewal_info, "renewalPrice", None)
    )
    event.currency = string_value(
        getattr(decoded_transaction, "currency", None)
    ) or string_value(getattr(decoded_renewal_info, "currency", None))
    event.last_received_at_ms = received_at_ms
    session.add(event)
    return event
