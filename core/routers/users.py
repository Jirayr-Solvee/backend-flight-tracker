import logging
import time
import uuid
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, and_, delete, select, update

from ..background_tasks import create_webhook_for_flight
from ..dependency import check_guest_auth_token, get_current_user
from ..models import get_session
from ..models.device import Device
from ..models.ai_consent import AIConsentReceipt, UserAIConsent, UserAIEmailIdentity
from ..models.flight import Flight, FlightRead
from ..models.live_activity import (
    LiveActivityPushToStartDelivery,
    LiveActivityPushToStartRegistration,
    LiveActivityRegistration,
)
from ..models.user import User, UserFlightLink
from ..security_logging import (
    CredentialOperation, CredentialSafeRoute, rollback_and_log_failure,
)
from ..services.apn.live_activity import LiveActivityService
from ..services.ai_consent import record_verified_apple_email_identity
from ..utils import create_jwt, verify_apple_identity_token

router = APIRouter(route_class=CredentialSafeRoute)

logger = logging.getLogger(__name__)


@router.get("/me/flights", response_model=list[FlightRead])
def get_user_flights(user: User = Depends(get_current_user)):
    return user.flights


class CreateGuesUserResponse(BaseModel):
    jwt: str = Field(repr=False)
    device_id: str
    guest_id: str


@router.post(
    "/me/guest",
    dependencies=[Depends(check_guest_auth_token)],
    response_model=CreateGuesUserResponse,
)
def create_guest_user(session: Session = Depends(get_session)):
    try:
        user_id = str(uuid.uuid4())
        device_id = str(uuid.uuid4())

        new_user = User(id=user_id)
        new_device = Device(id=device_id, user_id=user_id)

        session.add(new_user)
        session.add(new_device)

        jwt = create_jwt(sub=new_user.id)

        session.commit()
        return CreateGuesUserResponse(
            jwt=jwt, device_id=new_device.id, guest_id=new_user.id
        )
    except Exception:
        rollback_and_log_failure(session, logger, CredentialOperation.GUEST_CREATE)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


class CreateUserRequest(BaseModel):
    apple_jwt: str = Field(repr=False)
    full_name: str | None = Field(default=None, repr=False)
    email: str | None = Field(default=None, repr=False)


class CreateUserResponse(BaseModel):
    jwt: str = Field(repr=False)
    user_id: str
    full_name: str | None = Field(default=None, repr=False)
    email: str | None = Field(default=None, repr=False)


@router.post("/me/", response_model=CreateUserResponse)
async def create_user(
    data: CreateUserRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        if user.verified:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Registed users only",
            )

        apple_token_parts = await verify_apple_identity_token(data.apple_jwt)
        apple_user_id = apple_token_parts.get("sub")
        if not apple_user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sub messing from APPLE JWT",
            )

        # apple_user = session.get(User, apple_user_id)
        apple_user = session.exec(
            select(User).where(User.apple_id == apple_user_id)
        ).first()
        if apple_user:
            record_verified_apple_email_identity(session, apple_user.id, apple_token_parts)
            session.commit()
            jwt = create_jwt(sub=apple_user.id)
            return CreateUserResponse(
                jwt=jwt,
                full_name=apple_user.full_name,
                email=apple_user.email,
                user_id=apple_user.id,
            )

        full_name = data.full_name
        email = data.email
        if not email:
            email = apple_token_parts.get("email")

        user.apple_id = apple_user_id
        user.full_name = full_name
        user.email = email
        user.verified = True

        session.flush()
        record_verified_apple_email_identity(session, user.id, apple_token_parts)

        jwt = create_jwt(sub=user.id)

        session.commit()

        return CreateUserResponse(
            jwt=jwt, full_name=user.full_name, email=user.email, user_id=user.id
        )
    except Exception:
        rollback_and_log_failure(session, logger, CredentialOperation.APPLE_SIGN_IN)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


class RefreshApnToken(BaseModel):
    device_id: str
    apn_token: str = Field(repr=False)
    supports_localized_push: bool = False
    # Missing means the original dictionary, never implicit support for new keys.
    localized_push_version: int = Field(default=1, strict=True, ge=0, le=2)


@router.put("/me/apn/refresh", response_model=dict)
def refresh_apn_token(
    data: RefreshApnToken,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        device = session.exec(select(Device).where(Device.id == data.device_id)).first()
        # device must be created before requesting a refresh
        if not device:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Device not found",
            )
        # disable it on every other device in case its still used somewhere else and that spisific device is not updated yet
        session.exec(
            update(Device)
            .where(
                and_(
                    Device.apn_token == data.apn_token,
                    Device.apn_token_active.is_(True),  # type: ignore
                    Device.id != data.device_id,
                )
            )
            .values(apn_token_active=False)
        )

        # at this opint of time teh only thing left is to activate it ( or transfer it into teh new user)
        device.apn_token = data.apn_token
        device.apn_token_active = True
        device.supports_localized_push = data.supports_localized_push
        device.localized_push_version = data.localized_push_version
        device.user_id = user.id
        session.add(device)
        session.commit()

        return {"detail": "APN token refreshed successfully"}
    except HTTPException:
        raise
    except Exception:
        rollback_and_log_failure(session, logger, CredentialOperation.APN_REFRESH)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


class RegisterLiveActivityRequest(BaseModel):
    device_id: str
    flight_id: int
    push_token: str = Field(min_length=32, max_length=512, repr=False)
    apns_environment: Literal["sandbox", "production"] = "production"
    uses_12_hour_time: bool = False

    @field_validator("push_token")
    @classmethod
    def validate_push_token(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) % 2 != 0:
            raise ValueError("push_token must contain full bytes")
        try:
            bytes.fromhex(normalized)
        except ValueError as error:
            raise ValueError("push_token must be hexadecimal") from error
        return normalized


class RegisterLiveActivityPushToStartRequest(BaseModel):
    device_id: str
    push_token: str = Field(min_length=32, max_length=512, repr=False)
    apns_environment: Literal["sandbox", "production"] = "production"
    uses_12_hour_time: bool = False

    @field_validator("push_token")
    @classmethod
    def validate_push_token(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) % 2 != 0:
            raise ValueError("push_token must contain full bytes")
        try:
            bytes.fromhex(normalized)
        except ValueError as error:
            raise ValueError("push_token must be hexadecimal") from error
        return normalized


@router.put("/me/live-activity-push-to-start", response_model=dict)
def register_live_activity_push_to_start(
    data: RegisterLiveActivityPushToStartRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Register or rotate this device's ActivityKit push-to-start token."""
    device = session.exec(
        select(Device).where(
            Device.id == data.device_id,
            Device.user_id == user.id,
        )
    ).first()
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )

    try:
        registration = session.get(
            LiveActivityPushToStartRegistration, data.device_id
        )
        now = int(time.time())
        if registration is None:
            registration = LiveActivityPushToStartRegistration(
                device_id=data.device_id,
                push_token=data.push_token,
                apns_environment=data.apns_environment,
                uses_12_hour_time=data.uses_12_hour_time,
                created_at=now,
                updated_at=now,
            )
        else:
            token_changed = registration.push_token != data.push_token
            registration.push_token = data.push_token
            registration.apns_environment = data.apns_environment
            registration.uses_12_hour_time = data.uses_12_hour_time
            registration.active = True
            registration.updated_at = now
            if token_changed:
                registration.last_started_flight_id = None
                registration.last_start_at = None
                registration.last_apns_status = None
                registration.last_apns_reason = None

        session.add(registration)
        session.commit()
        logger.info(
            "Live Activity push-to-start token registered: device_id=%s environment=%s",
            data.device_id,
            data.apns_environment,
        )
        return {"detail": "Live Activity push-to-start token registered"}
    except Exception:
        rollback_and_log_failure(
            session, logger, CredentialOperation.ACTIVITY_START_REGISTER
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


@router.put("/me/live-activities/{activity_id}", response_model=dict)
def register_live_activity(
    activity_id: str,
    data: RegisterLiveActivityRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Register or rotate the ActivityKit token for one tracked flight."""
    if not activity_id or len(activity_id) > 128:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid activity id",
        )

    device = session.exec(
        select(Device).where(
            Device.id == data.device_id,
            Device.user_id == user.id,
        )
    ).first()
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )

    flight_link = session.exec(
        select(UserFlightLink).where(
            UserFlightLink.user_id == user.id,
            UserFlightLink.flight_id == data.flight_id,
        )
    ).first()
    if flight_link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tracked flight not found",
        )
    flight = session.get(Flight, data.flight_id)
    if flight is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flight not found",
        )

    try:
        registration = session.get(LiveActivityRegistration, activity_id)
        if registration is not None and registration.device_id != data.device_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Activity belongs to another device",
            )

        session.exec(
            update(LiveActivityRegistration)
            .where(
                and_(
                    LiveActivityRegistration.push_token == data.push_token,
                    LiveActivityRegistration.activity_id != activity_id,
                )
            )
            .values(active=False, updated_at=int(time.time()))
        )

        now = int(time.time())
        if registration is None:
            registration = LiveActivityRegistration(
                activity_id=activity_id,
                push_token=data.push_token,
                flight_id=data.flight_id,
                device_id=data.device_id,
                apns_environment=data.apns_environment,
                uses_12_hour_time=data.uses_12_hour_time,
                created_at=now,
                updated_at=now,
            )
        else:
            delivery_preferences_changed = (
                registration.push_token != data.push_token
                or registration.flight_id != data.flight_id
                or registration.apns_environment != data.apns_environment
                or registration.uses_12_hour_time != data.uses_12_hour_time
            )
            registration.push_token = data.push_token
            registration.flight_id = data.flight_id
            registration.apns_environment = data.apns_environment
            registration.uses_12_hour_time = data.uses_12_hour_time
            registration.active = True
            registration.updated_at = now
            if delivery_preferences_changed:
                registration.last_content_state_json = None

        session.add(registration)
        push_start_delivery = session.get(
            LiveActivityPushToStartDelivery,
            (data.device_id, data.flight_id),
        )
        if push_start_delivery is not None:
            push_start_delivery.state = "confirmed"
            push_start_delivery.confirmed_at = now
            push_start_delivery.updated_at = now
            session.add(push_start_delivery)
            logger.info(
                "Live Activity push-to-start confirmed: device_id=%s flight_id=%s",
                data.device_id,
                data.flight_id,
            )
        session.commit()
        background_tasks.add_task(
            create_webhook_for_flight,
            flight.number,
            data.flight_id,
        )
        background_tasks.add_task(
            LiveActivityService.send_updates_for_flight, data.flight_id
        )
        logger.info(
            "Live Activity registered: activity_id=%s flight_id=%s device_id=%s environment=%s",
            activity_id,
            data.flight_id,
            data.device_id,
            data.apns_environment,
        )
        return {"detail": "Live Activity registered"}
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        rollback_and_log_failure(session, logger, CredentialOperation.ACTIVITY_REGISTER)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


@router.delete(
    "/me/live-activities/{activity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def unregister_live_activity(
    activity_id: str,
    device_id: str = Query(...),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    registration = session.get(LiveActivityRegistration, activity_id)
    if registration is None:
        return None

    device = session.exec(
        select(Device).where(
            Device.id == device_id,
            Device.user_id == user.id,
        )
    ).first()
    if device is None or registration.device_id != device_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Live Activity not found",
        )

    registration.active = False
    registration.updated_at = int(time.time())
    session.add(registration)
    session.commit()
    logger.info(
        "Live Activity unregistered: activity_id=%s device_id=%s",
        activity_id,
        device_id,
    )
    return None


@router.get("/me/reset-notification")
def clear_user_notification(
    session: Session = Depends(get_session), user: User = Depends(get_current_user)
):
    try:
        user.notification_count = 0
        session.add(user)
        session.commit()
    except Exception:
        rollback_and_log_failure(session, logger, CredentialOperation.NOTIFICATION_CLEAR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


@router.delete("/me/")
def delete_user(
    session: Session = Depends(get_session), user: User = Depends(get_current_user)
):
    try:
        from ..services.notification_analytics import remove_notification_diagnostics
        remove_notification_diagnostics(session, user.id)
        device_ids = [device.id for device in user.devices]
        if device_ids:
            live_activities = session.exec(
                select(LiveActivityRegistration).where(
                    LiveActivityRegistration.device_id.in_(device_ids)  # type: ignore[attr-defined]
                )
            ).all()
            for live_activity in live_activities:
                session.delete(live_activity)

        for device in user.devices:
            session.delete(device)

        user.subscriptions.clear()
        user.flights.clear()

        session.exec(delete(AIConsentReceipt).where(AIConsentReceipt.user_id == user.id))
        session.exec(delete(UserAIConsent).where(UserAIConsent.user_id == user.id))
        session.exec(delete(UserAIEmailIdentity).where(UserAIEmailIdentity.user_id == user.id))

        session.flush()
        session.delete(user)
        session.commit()
    except Exception:
        rollback_and_log_failure(session, logger, CredentialOperation.ACCOUNT_DELETE)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
