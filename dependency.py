import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from .config import settings
from .models import get_session
from .models.user import User
from .utils import decode_jwt

user_security = HTTPBearer(
    scheme_name="User Bearer Token", description="Enter your firebase Bearer token"
)

lambda_security = HTTPBearer(
    scheme_name="Lambda Bearer Token", description="Enter your lambda Bearer token"
)

guest_security = HTTPBearer(
    scheme_name="Guest Bearer Token", description="Enter your Guest Bearer token"
)


def get_current_user(
    session: Session = Depends(get_session),
    credentials: HTTPAuthorizationCredentials = Depends(user_security),
) -> User:
    """
    Get current user after validation or create one
    Upgrade guest user into regestred one if status changed and user already in database
    """
    token = credentials.credentials

    try:
        decoded_token = decode_jwt(token)
        uid = decoded_token.get("sub")

        if not uid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization required",
            )

        # get user
        user = session.get(User, uid)

        if user:
            return user

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization required",
        )
    except Exception:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization required",
        )


def check_lambda_auth_token(
    credentials: HTTPAuthorizationCredentials = Depends(lambda_security),
):
    """
    Check and verfy lambda function token against token from env
    Opted for a hard coded token vs real JWT to save run time of lambda function
    """
    if credentials.credentials != settings.LAMBDA_FUNCTION_AUTH_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


def check_guest_auth_token(
    credentials: HTTPAuthorizationCredentials = Depends(guest_security),
):
    if credentials.credentials != settings.GUEST_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
