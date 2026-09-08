"""Bounded failure diagnostics for code handling credentials or account data."""

import logging
from enum import Enum

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlmodel import Session
from starlette.exceptions import HTTPException as StarletteHTTPException


class CredentialOperation(Enum):
    GUEST_CREATE = "guest_create"
    APPLE_SIGN_IN = "apple_sign_in"
    AUTHENTICATE = "authenticate"
    APN_REFRESH = "apn_refresh"
    ACTIVITY_START_REGISTER = "activity_start_register"
    ACTIVITY_REGISTER = "activity_register"
    NOTIFICATION_CLEAR = "notification_clear"
    ACCOUNT_DELETE = "account_delete"


def rollback_and_log_failure(
    session: Session, logger: logging.Logger, operation: CredentialOperation
) -> None:
    # Exception messages/chains and SQL parameters may contain tokens or PII.
    # Even rollback may raise one: never let it escape into a server traceback.
    rollback_failed = False
    try:
        session.rollback()
    except Exception:
        rollback_failed = True
    logger.error(
        "credential_operation_failed operation=%s rollback_failed=%s",
        operation.value,
        rollback_failed,
        exc_info=False,
        stack_info=False,
    )


class CredentialSafeRoute(APIRoute):
    """Keep unexpected account-handler/dependency errors out of server traces."""

    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def credential_safe_handler(request: Request):
            try:
                return await original_handler(request)
            except (StarletteHTTPException, RequestValidationError):
                # Existing deliberate status codes and static error text survive.
                raise
            except Exception:
                logging.getLogger(__name__).error(
                    "credential_request_failed", exc_info=False, stack_info=False
                )
                raise HTTPException(500, "Internal server error") from None

        return credential_safe_handler
