"""Trusted direct-SES intake. Legacy S3 events never authorize email processing.

Package core/email_ingress_contract.py verbatim at ZIP root beside this file.
All configuration, including the existing credential, is supplied in-memory by
the deployment operator to Lambda environment variables, never source literals.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

import boto3
from botocore.config import Config

from email_ingress_contract import (
    DEFAULT_KEY_PREFIX, MAX_EMAIL_BYTES, EmailIngressConfig, EmailIngressRejected,
    bind_object, extract_ses_receipt, validate_notification,
)

logger = logging.getLogger(__name__)
# Emit the bounded intake outcome without enabling SDK or root debug output.
logger.setLevel(logging.INFO)


class DeliveryBudget:
    """Synchronous soft work budget, not an interrupt or SES deadline guarantee."""

    def __init__(self, context, started_at):
        self.context = context
        self.deadline = started_at + 20
        self.remaining()

    def remaining(self):
        try:
            runtime_ms = self.context.get_remaining_time_in_millis()
            if type(runtime_ms) is not int:
                raise RuntimeError("Email intake delivery failed")
            remaining = min(self.deadline - time.monotonic(), runtime_ms / 1000 - 2)
            if remaining <= 0:
                raise RuntimeError("Email intake delivery failed")
            return remaining
        except Exception:
            raise RuntimeError("Email intake delivery failed") from None

    def timeout(self, maximum):
        return min(maximum, self.remaining())


def read_email_bytes(stream, size, budget):
    if budget is None:
        data = stream.read(MAX_EMAIL_BYTES + 1)
    else:
        chunks, total = [], 0
        while total < MAX_EMAIL_BYTES + 1:
            budget.remaining()
            set_timeout = getattr(stream, "set_socket_timeout", None)
            if callable(set_timeout):
                set_timeout(budget.timeout(3))
            requested = min(64 * 1024, MAX_EMAIL_BYTES + 1 - total)
            budget.remaining()
            chunk = stream.read(requested)
            budget.remaining()
            if not isinstance(chunk, bytes) or len(chunk) > requested:
                raise EmailIngressRejected()
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
    if len(data) != size:
        raise EmailIngressRejected()
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the fixed-origin bearer credential to any redirect.
        return None


def lambda_handler(event, context):
    started_at = time.monotonic()
    try:
        config = EmailIngressConfig(
            bucket=os.environ.get("FORWARDED_EMAIL_BUCKET", ""),
            key_prefix=os.environ.get("FORWARDED_EMAIL_KEY_PREFIX", DEFAULT_KEY_PREFIX),
            recipient=os.environ.get("FORWARDED_EMAIL_RECIPIENT", "track@sofly.to"),
        )
        proof = extract_ses_receipt(
            event, config,
            function_arn=os.environ.get("FORWARDED_EMAIL_LAMBDA_ARN", ""),
            invoked_function_arn=getattr(context, "invoked_function_arn", ""),
        )
        backend_url = os.environ.get("BACKEND_URL", "")
        token = os.environ.get("LAMBDA_FUNCTION_AUTH_TOKEN", "")
        if backend_url != "https://api.sofly.to" or not token or len(token) > 16384 or any(c.isspace() for c in token):
            raise EmailIngressRejected()
    except Exception:
        logger.warning("lambda_email_receipt_rejected")
        return {"disposition": "STOP_RULE_SET"}

    try:
        # Only inspect the mode after the complete exact SES action was validated.
        invocation_type = event["Records"][0]["ses"]["receipt"]["action"]["invocationType"]
        budget = DeliveryBudget(context, started_at) if invocation_type == "RequestResponse" else None
        key = config.key_prefix + proof["message_id"]
        s3 = boto3.client("s3", region_name="eu-north-1", config=Config(
            connect_timeout=budget.timeout(3) if budget else 3,
            read_timeout=budget.timeout(3) if budget else 10,
            retries={"total_max_attempts": 1},
        ))
        if budget:
            budget.remaining()
        obj = s3.get_object(Bucket=config.bucket, Key=key)
        stream = obj["Body"]
        try:
            size = obj.get("ContentLength")
            if type(size) is not int or not 0 < size <= MAX_EMAIL_BYTES:
                raise EmailIngressRejected()
            data = read_email_bytes(stream, size, budget)
        finally:
            stream.close()
        version_id = obj.get("VersionId")
        if version_id == "null":
            version_id = None
        proof = bind_object(proof, data, obj.get("ETag"), version_id)
        payload = {"bucket": config.bucket, "key": key, "receipt": proof}
        validate_notification(payload, config)
        if budget:
            budget.remaining()
        request = urllib.request.Request(
            backend_url + "/emails/", data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=budget.timeout(10) if budget else 10,
        ) as response:
            if response.status not in (200, 202):
                raise RuntimeError("Email intake delivery failed")
        if budget:
            budget.remaining()
        # Backend acceptance is not an import, Gemini-send or notification proof.
        logger.info("lambda_email_intake_accepted")
        return {"disposition": "STOP_RULE_SET"}
    except EmailIngressRejected:
        if budget:
            budget.remaining()
        logger.warning("lambda_email_receipt_rejected")
        return {"disposition": "STOP_RULE_SET"}
    except urllib.error.HTTPError as error:
        code = error.code
        try:
            error.close()
        except Exception:
            raise RuntimeError("Email intake delivery failed") from None
        if budget:
            budget.remaining()
        if 400 <= code < 500 and code not in (408, 429):
            logger.warning("lambda_email_intake_rejected")
            return {"disposition": "STOP_RULE_SET"}
        # A lost HTTP ACK may follow backend scheduling. Repeated delivery may
        # enqueue again, but the durable claim admits one processing winner.
        # Synchronous SES retry/SMTP behavior
        # after this operational error is not established by the Lambda API.
        raise RuntimeError("Email intake delivery failed") from None
    except Exception:
        raise RuntimeError("Email intake delivery failed") from None
