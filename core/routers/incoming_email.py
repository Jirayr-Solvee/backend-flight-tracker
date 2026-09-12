from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError

from ..background_tasks import handle_incoming_email
from ..dependency import check_lambda_auth_token
from ..models.email import S3EmailNotification
from ..email_ingress_contract import EmailIngressRejected
from ..security_logging import CredentialSafeRoute
from ..services.email_ingress import configured_ingress, validate_intake


class EmailSafeRoute(CredentialSafeRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Validation input can contain arbitrary sender/body/key values.
                raise HTTPException(422, {"code": "email_intake_invalid_request"}) from None
        return safe_handler


router = APIRouter(route_class=EmailSafeRoute)


@router.post("/", status_code=202, dependencies=[Depends(check_lambda_auth_token)])
def handle_incoming_email_notification(
    notification: S3EmailNotification, background_tasks: BackgroundTasks
):
    """
    Handles incoming emails from a lambda function and return asap (cost matter)
    """
    try:
        configured_ingress().validate()
    except EmailIngressRejected:
        raise HTTPException(503, {"code": "email_intake_unavailable"}) from None
    try:
        validate_intake(notification)
    except EmailIngressRejected:
        raise HTTPException(403, {"code": "email_receipt_rejected"}) from None
    background_tasks.add_task(handle_incoming_email, notification)
    return {"detail": "accepted"}
