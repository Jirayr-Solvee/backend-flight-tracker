import logging
import re
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field as PydanticField, model_validator
from sqlmodel import select

from ..dependency import check_lambda_auth_token, get_current_user
from ..models import Session, get_session
from ..models.activation_recovery import PurchaseActivationRecovery
from ..models.experiment import (
    ExperimentConversion,
    ExperimentExposure,
    ExperimentGoalSelection,
    current_time_ms,
)
from ..models.subscription import Subscription
from ..models.subscription_lifecycle import AppStoreSubscriptionLifecycleEvent
from ..models.transaction import Transaction
from ..models.user import User, UserSubscriptionLink
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


ActivationGoalKey = Literal[
    "gate_delay_alerts",
    "family_friends",
    "copilot_insights",
    "flight_history",
]


class ExperimentGoalSelectionRequest(BaseModel):
    experiment: ExperimentContext
    selected_goal_keys: list[ActivationGoalKey] = PydanticField(
        min_length=1,
        max_length=4,
    )
    selected_at_ms: int = PydanticField(ge=0)

    @model_validator(mode="after")
    def validate_goal_keys(self):
        if len(set(self.selected_goal_keys)) != len(self.selected_goal_keys):
            raise ValueError("selected_goal_keys must be unique")
        self.selected_goal_keys = sorted(self.selected_goal_keys)
        return self


class CreateTransactionRequest(BaseModel):
    jws_payload: str
    experiment: ExperimentContext | None = None


class ActivationRecoveryRequest(BaseModel):
    transaction_id: str = PydanticField(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    state: Literal["recovery_pending", "resolved"]
    flight_id: int | None = PydanticField(default=None, ge=1)
    failure_reason: str | None = PydanticField(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9_]+$",
    )

    @model_validator(mode="after")
    def validate_state_details(self):
        if self.state == "recovery_pending" and self.failure_reason is None:
            raise ValueError("failure_reason is required while recovery is pending")
        if self.state == "resolved":
            self.failure_reason = None
        return self


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
        source_priority = {
            "purchase_registration": 0,
            "onboarding_goal_selection": 1,
            "onboarding_exposure": 2,
        }
        if source_priority.get(source, 0) > source_priority.get(existing.source, 0):
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


@router.post("/experiments/goals")
def report_experiment_goal_selection(
    data: ExperimentGoalSelectionRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    context = data.experiment
    if not context.eligible or context.variant != "treatment_simplified":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Goal selection is only valid for the eligible treatment",
        )
    if context.exposed_at_ms is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Exposure timestamp is required",
        )

    exposure_id = _canonical_exposure_id(context)
    canonical_keys = ",".join(data.selected_goal_keys)
    try:
        _upsert_experiment_exposure(
            context=context,
            user=user,
            session=session,
            source="onboarding_goal_selection",
        )
        existing = session.get(ExperimentGoalSelection, exposure_id)
        if existing:
            if (
                existing.experiment_id != context.experiment_id
                or existing.variant != context.variant
                or existing.installation_id != str(context.installation_id)
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Experiment assignment conflict",
                )
            existing.selected_goal_keys = canonical_keys
            existing.selected_at_ms = data.selected_at_ms
            existing.last_reported_at_ms = current_time_ms()
            session.add(existing)
        else:
            session.add(
                ExperimentGoalSelection(
                    id=exposure_id,
                    experiment_id=context.experiment_id,
                    variant=context.variant,
                    eligible=context.eligible,
                    installation_id=str(context.installation_id),
                    exposure_id=exposure_id,
                    app_version=context.app_version,
                    build_number=context.build_number,
                    analytics_environment=context.analytics_environment,
                    user_id=user.id,
                    selected_goal_keys=canonical_keys,
                    selected_at_ms=data.selected_at_ms,
                )
            )
        session.commit()
        return {"detail": "success"}
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        logger.exception(
            "Unable to record experiment goal selection user_id=%s",
            user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


def _verified_transaction_for_user(
    transaction_id: str,
    user: User,
    session: Session,
) -> Transaction:
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Verified transaction was not found",
        )

    subscription_link = session.exec(
        select(UserSubscriptionLink).where(
            UserSubscriptionLink.user_id == user.id,
            UserSubscriptionLink.subscription_id == transaction.subscription_id,
        )
    ).first()
    if transaction.app_account_token != user.id and subscription_link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Verified transaction was not found",
        )
    return transaction


@router.post("/activation-recovery")
def report_activation_recovery(
    data: ActivationRecoveryRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Track whether a verified purchase is still waiting for its flight."""

    try:
        transaction = _verified_transaction_for_user(
            data.transaction_id,
            user,
            session,
        )
        if _enum_value(transaction.environment).casefold() != "production":
            return {"detail": "ignored_non_production"}

        recovery = session.get(PurchaseActivationRecovery, data.transaction_id)
        current_time = int(time.time())

        if data.state == "resolved":
            if recovery is None:
                return {"detail": "already_resolved"}
            recovery.state = "resolved"
            recovery.resolved_at = recovery.resolved_at or current_time
            recovery.last_reported_at = current_time
            recovery.failure_reason = None
            if data.flight_id is not None:
                recovery.flight_id = data.flight_id
            session.add(recovery)
            session.commit()
            return {"detail": "resolved"}

        if recovery is not None and recovery.state == "resolved":
            return {"detail": "already_resolved"}

        conversion = session.get(ExperimentConversion, data.transaction_id)
        if recovery is None:
            recovery = PurchaseActivationRecovery(
                transaction_id=data.transaction_id,
                original_transaction_id=transaction.subscription_id,
                user_id=user.id,
                first_pending_at=current_time,
                alert_due_at=current_time + 300,
                last_reported_at=current_time,
            )

        recovery.state = "recovery_pending"
        recovery.last_reported_at = current_time
        recovery.failure_reason = data.failure_reason
        if data.flight_id is not None:
            recovery.flight_id = data.flight_id
        if conversion is not None:
            recovery.experiment_variant = conversion.variant
            recovery.app_version = conversion.conversion_app_version
            recovery.build_number = conversion.conversion_build_number

        session.add(recovery)
        session.commit()
        return {
            "detail": "recovery_pending",
            "alert_due_at": recovery.alert_due_at,
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        logger.exception(
            "Unable to record activation recovery user_id=%s transaction_id=%s",
            user.id,
            data.transaction_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.get(
    "/activation-recovery/alerts",
    dependencies=[Depends(check_lambda_auth_token)],
)
def get_activation_recovery_alerts(
    include_resolved: bool = Query(default=False),
    session: Session = Depends(get_session),
):
    statement = select(PurchaseActivationRecovery).where(
        PurchaseActivationRecovery.alerted_at.is_not(None)
    )
    if not include_resolved:
        statement = statement.where(
            PurchaseActivationRecovery.state == "recovery_pending"
        )

    recoveries = session.exec(
        statement.order_by(PurchaseActivationRecovery.alerted_at.desc())
    ).all()
    return {
        "count": len(recoveries),
        "alerts": [
            {
                "transaction_id": recovery.transaction_id,
                "user_id": recovery.user_id,
                "flight_id": recovery.flight_id,
                "failure_reason": recovery.failure_reason,
                "experiment_variant": recovery.experiment_variant,
                "app_version": recovery.app_version,
                "build_number": recovery.build_number,
                "first_pending_at": recovery.first_pending_at,
                "alerted_at": recovery.alerted_at,
                "state": recovery.state,
                "resolved_at": recovery.resolved_at,
            }
            for recovery in recoveries
        ],
    }


@router.get(
    "/experiments/{experiment_id}/goal-summary",
    dependencies=[Depends(check_lambda_auth_token)],
)
def get_experiment_goal_summary(
    experiment_id: str,
    app_version: str | None = Query(default=None, max_length=40),
    session: Session = Depends(get_session),
):
    statement = select(ExperimentGoalSelection).where(
        ExperimentGoalSelection.experiment_id == experiment_id,
        ExperimentGoalSelection.eligible == True,  # noqa: E712
        ExperimentGoalSelection.analytics_environment == "production",
    )
    if app_version:
        statement = statement.where(
            ExperimentGoalSelection.app_version == app_version
        )

    selections = session.exec(statement).all()
    variants = sorted({selection.variant for selection in selections})
    arms = []
    for variant in variants:
        arm_selections = [
            selection
            for selection in selections
            if selection.variant == variant
        ]
        responding_installations = len(
            {selection.installation_id for selection in arm_selections}
        )
        choice_installations: dict[str, set[str]] = {
            key: set()
            for key in (
                "gate_delay_alerts",
                "family_friends",
                "copilot_insights",
                "flight_history",
            )
        }
        combination_installations: dict[tuple[str, ...], set[str]] = {}
        for selection in arm_selections:
            keys = tuple(
                key
                for key in selection.selected_goal_keys.split(",")
                if key
            )
            combination_installations.setdefault(keys, set()).add(
                selection.installation_id
            )
            for key in keys:
                choice_installations.setdefault(key, set()).add(
                    selection.installation_id
                )

        choices = [
            {
                "choice_key": key,
                "selected_installations": len(installations),
                "selection_rate": (
                    round(len(installations) / responding_installations, 4)
                    if responding_installations
                    else None
                ),
            }
            for key, installations in choice_installations.items()
        ]
        choices.sort(
            key=lambda item: (-item["selected_installations"], item["choice_key"])
        )

        combinations = [
            {
                "choice_keys": list(keys),
                "installations": len(installations),
                "selection_rate": (
                    round(len(installations) / responding_installations, 4)
                    if responding_installations
                    else None
                ),
            }
            for keys, installations in combination_installations.items()
        ]
        combinations.sort(
            key=lambda item: (-item["installations"], item["choice_keys"])
        )
        arms.append(
            {
                "variant": variant,
                "responding_installations": responding_installations,
                "choices": choices,
                "combinations": combinations,
            }
        )

    return {
        "experiment_id": experiment_id,
        "app_version": app_version,
        "analytics_environment": "production",
        "arms": arms,
    }


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
    exposure_variants = {
        exposure.installation_id: exposure.variant for exposure in exposures
    }

    assignments: dict[str, str] = {}
    tracked_subscriptions: dict[str, set[str]] = {}
    for conversion in conversions:
        if exposure_variants.get(conversion.installation_id) != conversion.variant:
            continue
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

    variants = sorted(
        {exposure.variant for exposure in exposures}
        | set(tracked_subscriptions)
        | set(metric_event_ids)
    )
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
