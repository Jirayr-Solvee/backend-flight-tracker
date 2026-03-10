import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, and_, select, update

from ..dependency import check_guest_auth_token, get_current_user
from ..models import get_session
from ..models.device import Device
from ..models.flight import FlightRead
from ..models.user import User
from ..utils import create_jwt, verify_apple_identity_token

router = APIRouter()

logger = logging.getLogger(__name__)


# TODO: remove
@router.get("/")
def get_all_users(session: Session = Depends(get_session)):
    users = session.exec(select(User)).all()
    return users


@router.get("/devices")
def get_all_users_devices(session: Session = Depends(get_session)):
    users = session.exec(select(Device)).all()
    return users


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
def create_user(
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

        apple_token_parts = verify_apple_identity_token(data.apple_jwt)
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
        device.user_id = user.id
        session.add(device)
        session.commit()

        return {"detail": "APN token refreshed successfully"}
    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logger.exception(
            f"Unable to update apn token for user user={user.id}, apn token={data.device_id}, apn token={data.apn_token}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


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
