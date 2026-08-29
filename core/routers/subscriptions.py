import logging
import re
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field as PydanticField, model_validator
from sqlmodel import select

from ..dependency import check_lambda_auth_token, get_current_user
from ..models import Session, get_session
from ..models.experiment import ExperimentConversion, ExperimentExposure
from ..models.subscription import Subscription
from ..models.subscription_lifecycle import AppStoreSubscriptionLifecycleEvent
from ..models.transaction import Transaction
from ..models.user import User
from ..services.app_store.service import AppStoreService
from ..services.revenue_measurement import upsert_verified_revenue_event
from ..services.subscription_lifecycle import lifecycle_metrics
from ..utils import calculate_premium_valid_until

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", dependencies=[Depends(check_lambda_auth_token)])
def get_all(session: Session = Depends(get_session)):
    subscriptions = session.exec(select(Subscription)).all()
    formatted = [
        {"sub": sub, "users": sub.users, "transactions": sub.transactions}
        for sub in subscriptions
    ]
    return formatted


class ExperimentContext(BaseModel):
    experiment_id: str = PydanticField(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9_]+$",
    )
    variant: str = PydanticField(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9_]+$",
    )
    eligible: bool
    installation_id: UUID
    exposure_id: str = PydanticField(min_length=1, max_length=140)
    app_version: str = PydanticField(min_length=1, max_length=40)
    build_number: str = PydanticField(min_length=1, max_length=40)
    analytics_environment: Literal["production", "development"]
    exposed_at_ms: int | None = PydanticField(default=None, ge=0)

    @model_validator(mode="after")
    def validate_exposure_id(self):
        expected = f"{self.experiment_id}:{self.installation_id}"
        if self.exposure_id.casefold() != expected.casefold():
            raise ValueError("exposure_id must match experiment_id and installation_id")
        return self


class CreateTransactionRequest(BaseModel):
    jws_payload: str
    experiment: ExperimentContext | None = None


def _enum_value(value) -> str:
    raw_value = getattr(value, "value", value)
    return str(raw_value) if raw_value is not None else "unknown"


def _canonical_exposure_id(context: ExperimentContext) -> str:
    return f"{context.experiment_id}:{context.installation_id}"


def _trial_duration_days(offer_period: str | None) -> int | None:
    match = re.fullmatch(r"P(?P<amount>\d+)(?P<unit>[DW])", offer_period or "")
    if not match:
        return None
    amount = int(match.group("amount"))
    return amount if match.group("unit") == "D" else amount * 7


def _upsert_experiment_exposure(
    *,
    context: ExperimentContext,
    user: User,
    session: Session,
    source: str,
) -> ExperimentExposure | None:
    if not context.eligible or context.exposed_at_ms is None:
        return None

    exposure_id = _canonical_exposure_id(context)
    existing = session.get(ExperimentExposure, exposure_id)
    if existing:
        if (
            existing.experiment_id != context.experiment_id
            or existing.variant != context.variant
            or existing.installation_id != str(context.installation_id)
        ):
            logger.warning(
                "Rejected experiment exposure mutation exposure_id=%s user_id=%s",
                exposure_id,
                user.id,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Experiment assignment conflict",
            )
        if existing.source == "purchase_registration" and source == "onboarding_exposure":
            existing.source = source
            session.add(existing)
        return existing

    exposure = ExperimentExposure(
        id=exposure_id,
        experiment_id=context.experiment_id,
        variant=context.variant,
        eligible=context.eligible,
        installation_id=str(context.installation_id),
        app_version=context.app_version,
        build_number=context.build_number,
        analytics_environment=context.analytics_environment,
        user_id=user.id,
        source=source,
        exposed_at_ms=context.exposed_at_ms,
    )
    session.add(exposure)
    return exposure


def _record_experiment_conversion(
    *,
    context: ExperimentContext,
    decoded_jws,
    user: User,
    session: Session,
) -> ExperimentConversion:
    transaction_id = str(decoded_jws.transactionId)
    existing = session.get(ExperimentConversion, transaction_id)
    if existing:
        return existing

    exposure_id = _canonical_exposure_id(context)
    exposure = session.get(ExperimentExposure, exposure_id)
    discount_type = _enum_value(decoded_jws.offerDiscountType)
    conversion = ExperimentConversion(
        id=transaction_id,
        original_transaction_id=str(decoded_jws.originalTransactionId),
        experiment_id=exposure.experiment_id if exposure else context.experiment_id,
        variant=exposure.variant if exposure else context.variant,
        eligible=exposure.eligible if exposure else context.eligible,
        installation_id=(
            exposure.installation_id if exposure else str(context.installation_id)
        ),
        exposure_id=exposure_id,
        app_version=exposure.app_version if exposure else context.app_version,
        build_number=exposure.build_number if exposure else context.build_number,
        conversion_app_version=context.app_version,
        conversion_build_number=context.build_number,
        analytics_environment=(
            exposure.analytics_environment
            if exposure
            else context.analytics_environment
        ),
        user_id=user.id,
        exposed_at_ms=exposure.exposed_at_ms if exposure else context.exposed_at_ms,
        product_id=str(decoded_jws.productId),
        purchase_environment=_enum_value(decoded_jws.environment),
        starts_trial=discount_type == "FREE_TRIAL",
        trial_duration_days=_trial_duration_days(decoded_jws.offerPeriod),
        purchase_date_ms=decoded_jws.purchaseDate,
    )
    session.add(conversion)
    return conversion


@router.post("/experiments/exposure")
def report_experiment_exposure(
    data: ExperimentContext,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        if data.exposed_at_ms is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Exposure timestamp is required",
            )
        _upsert_experiment_exposure(
            context=data,
            user=user,
            session=session,
            source="onboarding_exposure",
        )
        session.commit()
        return {"detail": "success"}
    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logger.exception("Unable to record experiment exposure user_id=%s", user.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.get(
    "/experiments/{experiment_id}/summary",
    dependencies=[Depends(check_lambda_auth_token)],
)
def get_experiment_summary(
    experiment_id: str,
    app_version: str | None = Query(default=None, max_length=40),
    session: Session = Depends(get_session),
):
    exposure_statement = select(ExperimentExposure).where(
        ExperimentExposure.experiment_id == experiment_id,
        ExperimentExposure.eligible == True,  # noqa: E712
        ExperimentExposure.analytics_environment == "production",
    )
    conversion_statement = select(ExperimentConversion).where(
        ExperimentConversion.experiment_id == experiment_id,
        ExperimentConversion.eligible == True,  # noqa: E712
        ExperimentConversion.analytics_environment == "production",
        ExperimentConversion.purchase_environment == "Production",
    )
    if app_version:
        exposure_statement = exposure_statement.where(
            ExperimentExposure.app_version == app_version
        )
        conversion_statement = conversion_statement.where(
            ExperimentConversion.app_version == app_version
        )

    exposures = session.exec(exposure_statement).all()
    conversions = session.exec(conversion_statement).all()
    variants = sorted({item.variant for item in [*exposures, *conversions]})
    arms = []
    for variant in variants:
        exposed_installations = {
            exposure.installation_id
            for exposure in exposures
            if exposure.variant == variant
        }
        arm_conversions = [
            conversion
            for conversion in conversions
            if conversion.variant == variant
            and conversion.installation_id in exposed_installations
        ]
        trial_installations = {
            conversion.installation_id
            for conversion in arm_conversions
            if conversion.starts_trial
        }
        purchase_installations = {
            conversion.installation_id for conversion in arm_conversions
        }
        exposure_count = len(exposed_installations)
        trial_count = len(trial_installations)
        purchase_count = len(purchase_installations)
        arms.append(
            {
                "variant": variant,
                "exposed_installations": exposure_count,
                "verified_trial_installations": trial_count,
                "verified_purchase_installations": purchase_count,
                "trial_conversion_rate": (
                    round(trial_count / exposure_count, 4) if exposure_count else None
                ),
                "purchase_conversion_rate": (
                    round(purchase_count / exposure_count, 4)
                    if exposure_count
                    else None
                ),
            }
        )

    return {
        "experiment_id": experiment_id,
        "app_version": app_version,
        "analytics_environment": "production",
        "purchase_environment": "Production",
        "arms": arms,
    }


LIFECYCLE_METRIC_NAMES = (
    "auto_renew_disabled",
    "auto_renew_enabled",
    "renewal",
    "expiration",
    "billing_failure",
    "grace_period_started",
    "grace_period_expired",
    "refund",
    "refund_reversed",
    "refund_declined",
)


@router.get(
    "/experiments/{experiment_id}/lifecycle-summary",
    dependencies=[Depends(check_lambda_auth_token)],
)
def get_experiment_lifecycle_summary(
    experiment_id: str,
    app_version: str | None = Query(default=None, max_length=40),
    session: Session = Depends(get_session),
):
    conversion_statement = select(ExperimentConversion).where(
        ExperimentConversion.experiment_id == experiment_id,
        ExperimentConversion.eligible == True,  # noqa: E712
        ExperimentConversion.analytics_environment == "production",
        ExperimentConversion.purchase_environment == "Production",
    )
    if app_version:
        conversion_statement = conversion_statement.where(
            ExperimentConversion.app_version == app_version
        )
    conversions = session.exec(conversion_statement).all()

    assignments: dict[str, str] = {}
    tracked_subscriptions: dict[str, set[str]] = {}
    for conversion in conversions:
        assignments.setdefault(conversion.original_transaction_id, conversion.variant)
        tracked_subscriptions.setdefault(conversion.variant, set()).add(
            conversion.original_transaction_id
        )

    events = session.exec(
        select(AppStoreSubscriptionLifecycleEvent).where(
            AppStoreSubscriptionLifecycleEvent.purchase_environment == "Production"
        )
    ).all()
    metric_event_ids: dict[str, dict[str, set[str]]] = {}
    metric_subscription_ids: dict[str, dict[str, set[str]]] = {}
    for event in events:
        if not event.original_transaction_id:
            continue
        variant = assignments.get(event.original_transaction_id)
        if not variant:
            continue
        for metric in lifecycle_metrics(event):
            metric_event_ids.setdefault(variant, {}).setdefault(metric, set()).add(
                event.id
            )
            metric_subscription_ids.setdefault(variant, {}).setdefault(
                metric, set()
            ).add(event.original_transaction_id)

    variants = sorted(set(tracked_subscriptions) | set(metric_event_ids))
    arms = []
    for variant in variants:
        arm_event_ids = metric_event_ids.get(variant, {})
        arm_subscription_ids = metric_subscription_ids.get(variant, {})
        arms.append(
            {
                "variant": variant,
                "tracked_subscriptions": len(
                    tracked_subscriptions.get(variant, set())
                ),
                "events": {
                    metric: len(arm_event_ids.get(metric, set()))
                    for metric in LIFECYCLE_METRIC_NAMES
                },
                "affected_subscriptions": {
                    metric: len(arm_subscription_ids.get(metric, set()))
                    for metric in LIFECYCLE_METRIC_NAMES
                },
            }
        )

    return {
        "experiment_id": experiment_id,
        "app_version": app_version,
        "purchase_environment": "Production",
        "arms": arms,
    }


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
            logger.warning("Invalid JWS payload from user id=%s", user.id)
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
        db_transaction.app_account_token = (
            str(decoded_jws.appAccountToken)
            if getattr(decoded_jws, "appAccountToken", None)
            else user.id
        )

        # now we need to link it to the actual user it self
        if db_subscription not in user.subscriptions:
            user.subscriptions.append(db_subscription)

        premium_until = calculate_premium_valid_until(decoded_jws.expiresDate)

        user.premium_valid_until = premium_until

        session.add(db_transaction)
        session.flush()

        upsert_verified_revenue_event(
            session=session,
            decoded_jws=decoded_jws,
            user_id=user.id,
        )

        if data.experiment:
            _upsert_experiment_exposure(
                context=data.experiment,
                user=user,
                session=session,
                source="purchase_registration",
            )
            _record_experiment_conversion(
                context=data.experiment,
                decoded_jws=decoded_jws,
                user=user,
                session=session,
            )

        session.commit()
        return {"detail": "successfull"}

    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logger.exception("Unable to create/update transaction for user id=%s", user.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
