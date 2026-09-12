"""Synthetic receipt fixtures only; not substitutes for live SES provenance."""

import hashlib
import io
from datetime import datetime, timezone
from email.message import EmailMessage
from uuid import uuid4

from core.email_ingress_contract import (
    DEFAULT_KEY_PREFIX, EmailIngressConfig, bind_object, now_ms,
)
from core.models.email import S3EmailNotification


BUCKET = "fixture-bucket"
SENDER = "synthetic-owner@example.invalid"
RECIPIENT = "track@sofly.to"
FUNCTION_ARN = "arn:aws:lambda:eu-north-1:123456789012:function:incoming_email_handler"
CONFIG = EmailIngressConfig(BUCKET, DEFAULT_KEY_PREFIX, RECIPIENT)


def raw_email(sender=SENDER, body="PRIVATE_SYNTHETIC_EMAIL_NOT_FOR_LOGS") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = RECIPIENT
    message.set_content(body)
    return message.as_bytes()


def etag(data: bytes) -> str:
    return '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'


def notification(data: bytes | None = None, *, sender=SENDER, received_at_ms=None,
                 message_id=None, version_id=None) -> S3EmailNotification:
    data = raw_email(sender) if data is None else data
    received = now_ms() - 1 if received_at_ms is None else received_at_ms
    identifier = str(uuid4()) if message_id is None else message_id
    proof = bind_object({
        "version": 1, "source": "ses_direct", "message_id": identifier,
        "received_at_ms": received, "sender": sender, "recipient": RECIPIENT,
        "dmarc": "PASS", "spam": "PASS", "virus": "PASS", "headers_truncated": False,
    }, data, etag(data), version_id)
    return S3EmailNotification(bucket=BUCKET, key=DEFAULT_KEY_PREFIX + identifier, receipt=proof)


def s3_object(data: bytes, *, version_id=None) -> dict:
    response = {"Body": io.BytesIO(data), "ETag": etag(data), "ContentLength": len(data)}
    if version_id is not None:
        response["VersionId"] = version_id
    return response


def ses_event(*, sender=SENDER, received_at_ms=None, message_id=None, action_delay_ms=0) -> dict:
    received = now_ms() - 100 if received_at_ms is None else received_at_ms
    timestamp = lambda value: datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds")
    return {"Records": [{"eventSource": "aws:ses", "eventVersion": "1.0", "ses": {
        "mail": {
            "timestamp": timestamp(received), "source": "bounce@envelope.example.invalid",
            "messageId": message_id or str(uuid4()), "destination": [RECIPIENT],
            "headersTruncated": False, "headers": [{"name": "From", "value": sender}],
            "commonHeaders": {"from": [sender], "messageId": "<attacker-mime-id@example.invalid>"},
        },
        "receipt": {
            "timestamp": timestamp(received + action_delay_ms), "recipients": [RECIPIENT],
            "spamVerdict": {"status": "PASS"}, "virusVerdict": {"status": "PASS"},
            "spfVerdict": {"status": "PASS"}, "dkimVerdict": {"status": "PASS"},
            "dmarcVerdict": {"status": "PASS"},
            "action": {"type": "Lambda", "functionArn": FUNCTION_ARN, "invocationType": "Event"},
        },
    }}]}
