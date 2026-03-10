from fastapi import APIRouter, BackgroundTasks, Depends

from ..background_tasks import handle_incoming_email
from ..dependency import check_lambda_auth_token
from ..models.email import S3EmailNotification

router = APIRouter()


@router.post("/", dependencies=[Depends(check_lambda_auth_token)])
def handle_incoming_email_notification(
    notification: S3EmailNotification, background_tasks: BackgroundTasks
):
    """
    Handles incoming emails from a lambda function and return asap (cost matter)
    """
    background_tasks.add_task(handle_incoming_email, notification)
    return {"detail": "ok"}
