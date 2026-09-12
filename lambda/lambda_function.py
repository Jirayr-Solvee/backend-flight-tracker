"""Trusted direct-SES intake. Legacy S3 events never authorize email processing.

Package core/email_ingress_contract.py verbatim at ZIP root beside this file.
All configuration, including the existing credential, is supplied in-memory by
the deployment operator to Lambda environment variables, never source literals.
"""

import json
import logging
import os
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the fixed-origin bearer credential to any redirect.
        return None


def lambda_handler(event, context):
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
        return {"statusCode": 403, "body": "email_receipt_rejected"}

    try:
        key = config.key_prefix + proof["message_id"]
        s3 = boto3.client("s3", region_name="eu-north-1", config=Config(
            connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1},
        ))
        obj = s3.get_object(Bucket=config.bucket, Key=key)
        stream = obj["Body"]
        try:
            size = obj.get("ContentLength")
            if type(size) is not int or not 0 < size <= MAX_EMAIL_BYTES:
                raise EmailIngressRejected()
            data = stream.read(MAX_EMAIL_BYTES + 1)
            if len(data) != size:
                raise EmailIngressRejected()
        finally:
            stream.close()
        version_id = obj.get("VersionId")
        if version_id == "null":
            version_id = None
        proof = bind_object(proof, data, obj.get("ETag"), version_id)
        payload = {"bucket": config.bucket, "key": key, "receipt": proof}
        validate_notification(payload, config)
        request = urllib.request.Request(
            backend_url + "/emails/", data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            if response.status not in (200, 202):
                raise RuntimeError("Email intake delivery failed")
        # Backend acceptance is not an import, Gemini-send or notification proof.
        logger.info("lambda_email_intake_accepted")
        return {"statusCode": 202, "body": "email_intake_accepted"}
    except EmailIngressRejected:
        logger.warning("lambda_email_receipt_rejected")
        return {"statusCode": 403, "body": "email_receipt_rejected"}
    except urllib.error.HTTPError as error:
        code = error.code
        try:
            error.close()
        except Exception:
            raise RuntimeError("Email intake delivery failed") from None
        if 400 <= code < 500 and code not in (408, 429):
            logger.warning("lambda_email_intake_rejected")
            return {"statusCode": code, "body": "email_intake_rejected"}
        # Retry the SAME trusted event. The durable backend claim prevents a
        # second SDK job after successful acceptance, including a lost HTTP ACK.
        raise RuntimeError("Email intake delivery failed") from None
    except Exception:
        raise RuntimeError("Email intake delivery failed") from None
