import logging
import time
import uuid
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, and_, select, update

from ..background_tasks import create_webhook_for_flight
from ..dependency import check_guest_auth_token, get_current_user
from ..models import get_session
from ..models.device import Device
from ..models.flight import Flight, FlightRead
from ..models.live_activity import LiveActivityRegistration
from ..models.user import User, UserFlightLink
from ..services.apn.live_activity import LiveActivityService
from ..utils import create_jwt, verify_apple_identity_token

router = APIRouter()

logger = logging.getLogger(__name__)


@router.get("/me/flights", response_model=list[FlightRead])
def get_user_flights(user: User = Depends(get_current_user)):
    return user.flights


class CreateGuesUserResponse(BaseModel):
    jwt: str
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
        session.rollback()
        logger.exception(f"unable to create a guest account due to following error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


class CreateUserRequest(BaseModel):
    apple_jwt: str
    full_name: str | None = None
    email: str | None = None


class CreateUserResponse(BaseModel):
    jwt: str
    user_id: str
    full_name: str | None = None
    email: str | None = None


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

        jwt = create_jwt(sub=user.id)

        session.commit()

        return CreateUserResponse(
            jwt=jwt, full_name=user.full_name, email=user.email, user_id=user.id
        )
    except Exception:
        session.rollback()
        logger.exception(
            f"Unable to create a user for guest user id={user.id}, data={data}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


class RefreshApnToken(BaseModel):
    device_id: str
    apn_token: str
    supports_localized_push: bool = False


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
        device.user_id = user.id
        session.add(device)
        session.commit()

        return {"detail": "APN token refreshed successfully"}
    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logger.exception(
            "Unable to update APNs token for user_id=%s device_id=%s",
            user.id,
            data.device_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


class RegisterLiveActivityRequest(BaseModel):
    device_id: str
    flight_id: int
    push_token: str = Field(min_length=32, max_length=512)
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
        session.rollback()
        logger.exception(
            "Unable to register Live Activity: activity_id=%s flight_id=%s device_id=%s",
            activity_id,
            data.flight_id,
            data.device_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


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
        session.rollback()
        logger.exception("Something went wrong while clearing user notification")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.delete("/me/")
def delete_user(
    session: Session = Depends(get_session), user: User = Depends(get_current_user)
):
    try:
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

        session.flush()
        session.delete(user)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(f"Something went wrong while deleting user id={user.id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
