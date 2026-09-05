import logging
import re
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field as PydanticField, model_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ..config import settings
from ..dependency import check_lambda_auth_token, get_current_user
from ..models import Session, get_session
from ..models.activation_recovery import PurchaseActivationRecovery
from ..models.experiment import (
    ExperimentConversion,
    ExperimentExposure,
    ExperimentGoalSelection,
    ExperimentEnrollment,
    current_time_ms,
)
from ..models.subscription import Subscription
from ..models.subscription_lifecycle import AppStoreSubscriptionLifecycleEvent
from ..models.transaction import Transaction
from ..models.user import User, UserSubscriptionLink
from ..services.app_store.service import AppStoreService
from ..services.revenue_measurement import refresh_current_entitlement, upsert_verified_transaction, upsert_verified_revenue_event
from ..services.experiment_reporting import experiment_summary
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
    analytics_environment: Literal["production", "development", "testflight"]
    exposed_at_ms: int | None = PydanticField(default=None, ge=0)
    measurement_revision: Literal[1, 2] | None = None

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


FlightDetailPaywallVariant = Literal[
    "control_current_paywall",
    "treatment_flight_detail_card",
]


class ExperimentAssignmentRequest(BaseModel):
    experiment_id: Literal["paywall_flight_detail_2026_09"]
    installation_id: UUID
    current_variant: FlightDetailPaywallVariant | None = None
    app_version: str = PydanticField(min_length=1, max_length=40)
    build_number: str = PydanticField(min_length=1, max_length=40)
    analytics_environment: Literal["production", "development", "testflight"]
    assignment_locked: bool = False
    measurement_revision: Literal[1, 2] | None = None


class ExperimentAssignmentResponse(BaseModel):
    experiment_id: str
    variant: FlightDetailPaywallVariant
    experiment_enabled: bool
    assignment_source: Literal[
        "deterministic_split",
        "existing_assignment",
        "forced_control",
        "forced_treatment",
        "disabled",
    ]
    config_version: str
    cache_ttl_seconds: int
    effective_variant: FlightDetailPaywallVariant


class ExperimentEnrollmentRequest(BaseModel):
    experiment: ExperimentContext
    measurement_revision: Literal[2]
    enrolled_at_ms: int = PydanticField(ge=0)
    effective_variant: FlightDetailPaywallVariant | None = None
    assignment_source: str | None = PydanticField(
        default=None, max_length=80, pattern=r"^[a-z0-9_]+$"
    )
    config_version: str | None = PydanticField(
        default=None, max_length=80, pattern=r"^[A-Za-z0-9._-]+$"
    )


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


def _stable_experiment_bucket(value: str) -> int:
    """Return a deterministic 0-99 bucket using the app's FNV-1a hash."""
    hash_value = 14_695_981_039_346_656_037
    for byte in value.encode("utf-8"):
        hash_value ^= byte
        hash_value = (hash_value * 1_099_511_628_211) & 0xFFFFFFFFFFFFFFFF
    return hash_value % 100


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
    enrollment = session.get(ExperimentEnrollment, f"{exposure_id}:v2")
    if enrollment and (
        enrollment.variant != context.variant
        or enrollment.analytics_environment != context.analytics_environment
    ):
        raise HTTPException(status_code=409, detail="Experiment assignment conflict")
    existing = session.get(ExperimentExposure, exposure_id)
    if existing:
        if (
            existing.experiment_id != context.experiment_id
            or existing.variant != context.variant
            or existing.installation_id != str(context.installation_id)
            or existing.analytics_environment != context.analytics_environment
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
    enrollment = session.get(ExperimentEnrollment, f"{exposure_id}:v2")
    cohort = enrollment or exposure
    discount_type = _enum_value(decoded_jws.offerDiscountType)
    conversion = ExperimentConversion(
        id=transaction_id,
        original_transaction_id=str(decoded_jws.originalTransactionId),
        experiment_id=cohort.experiment_id if cohort else context.experiment_id,
        variant=cohort.variant if cohort else context.variant,
        eligible=cohort.eligible if cohort else context.eligible,
        installation_id=(
            cohort.installation_id if cohort else str(context.installation_id)
        ),
        exposure_id=exposure_id,
        app_version=cohort.app_version if cohort else context.app_version,
        build_number=cohort.build_number if cohort else context.build_number,
        conversion_app_version=context.app_version,
        conversion_build_number=context.build_number,
        analytics_environment=(
            cohort.analytics_environment
            if cohort
            else context.analytics_environment
        ),
        user_id=user.id,
        exposed_at_ms=(enrollment.enrolled_at_ms if enrollment else
                       (exposure.exposed_at_ms if exposure else context.exposed_at_ms)),
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


@router.post("/experiments/assignment")
def get_experiment_assignment(
    data: ExperimentAssignmentRequest,
    _user: User = Depends(get_current_user),
) -> ExperimentAssignmentResponse:
    mode = settings.FLIGHT_DETAIL_PAYWALL_EXPERIMENT_MODE
    treatment_percent = min(
        100,
        max(0, settings.FLIGHT_DETAIL_PAYWALL_TREATMENT_PERCENT),
    )

    if mode == "off":
        variant: FlightDetailPaywallVariant = "control_current_paywall"
        enabled = False
        source = "disabled"
    elif mode == "control":
        variant = "control_current_paywall"
        enabled = True
        source = "forced_control"
    elif mode == "treatment":
        variant = "treatment_flight_detail_card"
        enabled = True
        source = "forced_treatment"
    elif data.current_variant is not None:
        # Preserve assignments made by earlier app builds while the experiment
        # remains in split mode. A force/off mode intentionally overrides them.
        variant = data.current_variant
        enabled = True
        source = "existing_assignment"
    else:
        bucket = _stable_experiment_bucket(
            f"{data.experiment_id}:{data.installation_id}"
        )
        variant = (
            "treatment_flight_detail_card"
            if bucket < treatment_percent
            else "control_current_paywall"
        )
        enabled = True
        source = "deterministic_split"

    effective_variant = variant
    # Once measured, variant is history. Force/off modes change delivery only.
    if data.assignment_locked and data.current_variant is not None:
        variant = data.current_variant

    return ExperimentAssignmentResponse(
        experiment_id=data.experiment_id,
        variant=variant,
        experiment_enabled=enabled,
        assignment_source=source,
        config_version=settings.FLIGHT_DETAIL_PAYWALL_CONFIG_VERSION,
        cache_ttl_seconds=max(
            0,
            settings.FLIGHT_DETAIL_PAYWALL_CACHE_TTL_SECONDS,
        ),
        effective_variant=effective_variant,
    )


@router.post("/experiments/enrollment")
def report_experiment_enrollment(
    data: ExperimentEnrollmentRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    context = data.experiment
    if (
        context.experiment_id != "paywall_flight_detail_2026_09"
        or context.variant not in (
            "control_current_paywall", "treatment_flight_detail_card"
        )
        or not context.eligible
        or context.measurement_revision not in (None, 2)
    ):
        raise HTTPException(status_code=422, detail="Invalid eligible enrollment")
    exposure_id = _canonical_exposure_id(context)
    enrollment_id = f"{exposure_id}:v2"
    existing = session.get(ExperimentEnrollment, enrollment_id)
    if existing:
        if (
            existing.variant != context.variant
            or existing.analytics_environment != context.analytics_environment
        ):
            raise HTTPException(status_code=409, detail="Experiment assignment conflict")
        return {"detail": "success", "measurement_revision": 2}

    # Delivery can be out of order: only a strictly earlier legacy exposure
    # proves this installation belonged to v1. A same/later paywall event can
    # legitimately arrive before its durable enrollment request.
    exposure = session.get(ExperimentExposure, exposure_id)
    if exposure and exposure.exposed_at_ms < data.enrolled_at_ms:
        raise HTTPException(status_code=409, detail="Installation already measured in revision 1")
    session.add(ExperimentEnrollment(
        id=enrollment_id,
        experiment_id=context.experiment_id,
        measurement_revision=2,
        variant=context.variant,
        effective_variant=data.effective_variant or context.variant,
        eligible=True,
        installation_id=str(context.installation_id),
        exposure_id=exposure_id,
        app_version=context.app_version,
        build_number=context.build_number,
        analytics_environment=context.analytics_environment,
        user_id=user.id,
        enrolled_at_ms=data.enrolled_at_ms,
        assignment_source=data.assignment_source,
        config_version=data.config_version,
    ))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.get(ExperimentEnrollment, enrollment_id)
        if (existing is None or existing.variant != context.variant
                or existing.analytics_environment != context.analytics_environment):
            raise HTTPException(status_code=409, detail="Experiment assignment conflict")
    return {"detail": "success", "measurement_revision": 2}


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
    measurement_revision: Literal[1, 2] | None = None,
    since_ms: int | None = None,
    until_ms: int | None = None,
    product_id: str | None = None,
    acquisition_source: Literal["apple_ads", "unknown"] | None = None,
    horizon_days: Literal[14, 30] = 14,
):
    if since_ms is not None and until_ms is not None and until_ms <= since_ms:
        raise HTTPException(status_code=422, detail="until_ms must be after since_ms")
    revision = measurement_revision or (2 if experiment_id == "paywall_flight_detail_2026_09" else 1)
    return experiment_summary(
        session=session, experiment_id=experiment_id, app_version=app_version,
        measurement_revision=revision, since_ms=since_ms, until_ms=until_ms,
        product_id=product_id, acquisition_source=acquisition_source,
        horizon_days=horizon_days,
    )


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
    measurement_revision: Literal[1, 2] | None = None,
):
    revision = measurement_revision or (2 if experiment_id == "paywall_flight_detail_2026_09" else 1)
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
    enrollments = session.exec(select(ExperimentEnrollment).where(
        ExperimentEnrollment.experiment_id == experiment_id,
        ExperimentEnrollment.eligible == True,  # noqa: E712
        ExperimentEnrollment.analytics_environment == "production",
    )).all()
    enrolled_installations = {item.installation_id for item in enrollments}
    if revision == 2:
        exposures = [item for item in enrollments if not app_version or item.app_version == app_version]
    else:
        exposures = [item for item in exposures if item.installation_id not in enrolled_installations]
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
        "measurement_revision": revision,
        "denominator": "selected_flight_enrollment" if revision == 2 else "legacy_paywall_exposure",
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

        db_transaction, _ = upsert_verified_transaction(
            session=session, decoded_jws=decoded_jws, fallback_user_id=user.id,
        )

        # now we need to link it to the actual user it self
        if db_subscription not in user.subscriptions:
            user.subscriptions.append(db_subscription)

        # Repeated verified registrations also refresh the bounded backend
        # entitlement cache, including a restore to a newly linked guest. Use
        # persisted current facts, never the possibly stale incoming snapshot.
        # Preserve an independently granted billing grace window.
        session.flush()
        refresh_current_entitlement(user=user, session=session)

        upsert_verified_revenue_event(
            session=session,
            decoded_jws=decoded_jws,
            user_id=user.id,
        )

        # Purchase success has its own commit boundary. Experiment conflicts or
        # transient metadata failures must never erase verified Apple facts.
        session.commit()

    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logger.exception("Unable to create/update transaction for user id=%s", user.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )

    if data.experiment:
        try:
            _upsert_experiment_exposure(
                context=data.experiment, user=user, session=session,
                source="purchase_registration",
            )
            _record_experiment_conversion(
                context=data.experiment, decoded_jws=decoded_jws,
                user=user, session=session,
            )
            session.commit()
        except HTTPException as error:
            session.rollback()
            return {"detail": "successfull", "experiment_tracking_status": (
                "conflict" if error.status_code == 409 else "pending"
            )}
        except Exception:
            session.rollback()
            logger.exception("Verified purchase metadata retry needed user_id=%s", user.id)
            return {"detail": "successfull", "experiment_tracking_status": "pending"}
    return {"detail": "successfull"}
