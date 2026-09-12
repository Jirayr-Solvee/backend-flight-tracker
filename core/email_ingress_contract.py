"""Pure-stdlib SES contract shared verbatim by the backend and Lambda package.

The receipt is trusted only from direct SES invocation restricted by AWS policy.
It is not a signature scheme for arbitrary callers or MIME authentication headers.
Never include exception input, sender, object keys or bytes in diagnostics.
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesHeaderParser, HeaderParser


MAX_EMAIL_BYTES = 20 * 1024 * 1024
MAX_RECEIPT_AGE_MS = 6 * 60 * 60 * 1000
DEFAULT_KEY_PREFIX = "authenticated-v1/"
MESSAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ETAG = re.compile(r'"[0-9a-fA-F]{32}(?:-[1-9][0-9]{0,5})?"\Z')
ADDRESS = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
PROOF_FIELDS = frozenset({
    "version", "source", "message_id", "received_at_ms", "sender", "recipient",
    "dmarc", "spam", "virus", "headers_truncated", "content_sha256", "etag",
    "version_id",
})


class EmailIngressRejected(Exception):
    def __init__(self):
        super().__init__("Email receipt rejected")


@dataclass(frozen=True, repr=False)
class EmailIngressConfig:
    bucket: str
    key_prefix: str
    recipient: str

    def validate(self):
        if not isinstance(self.bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket):
            raise EmailIngressRejected()
        # Only the separately protected, new prefix may authorize email AI.
        if self.key_prefix != DEFAULT_KEY_PREFIX or self.recipient != "track@sofly.to":
            raise EmailIngressRejected()


def now_ms() -> int:
    return int(time.time() * 1000)


def normalized_address(value: str) -> str:
    if not isinstance(value, str) or not 3 <= len(value) <= 320 or not ADDRESS.fullmatch(value):
        raise EmailIngressRejected()
    return value.casefold()


def single_from_header(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or "\r" in value or "\n" in value:
        raise EmailIngressRejected()
    try:
        message = HeaderParser(policy=policy.default).parsestr("From: " + value + "\n\n")
        header = message["From"]
        if message.defects or header.defects or len(header.addresses) != 1:
            raise EmailIngressRejected()
        if any(group.display_name is not None for group in header.groups):
            raise EmailIngressRejected()
        return normalized_address(header.addresses[0].addr_spec)
    except Exception:
        raise EmailIngressRejected() from None


def mime_sender(data: bytes) -> str:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_EMAIL_BYTES:
        raise EmailIngressRejected()
    try:
        message = BytesHeaderParser(policy=policy.default).parsebytes(data)
        headers = message.get_all("From", [])
        if message.defects or len(headers) != 1 or headers[0].defects:
            raise EmailIngressRejected()
        return single_from_header(str(headers[0]))
    except Exception:
        raise EmailIngressRejected() from None


def _timestamp(value) -> int:
    if not isinstance(value, str) or len(value) > 40:
        raise EmailIngressRejected()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise EmailIngressRejected()
        return int(parsed.timestamp() * 1000)
    except Exception:
        raise EmailIngressRejected() from None


def require_fresh_receipt(received_at_ms: int, *, at_ms: int | None = None) -> None:
    current = now_ms() if at_ms is None else at_ms
    if type(received_at_ms) is not int or not 0 <= current - received_at_ms <= MAX_RECEIPT_AGE_MS:
        # Reject future timestamps too: receipt time must precede authorization.
        raise EmailIngressRejected()


def extract_ses_receipt(event, config: EmailIngressConfig, *, function_arn: str,
                        invoked_function_arn: str, at_ms: int | None = None) -> dict:
    """Only trusted event metadata establishes DMARC; never inspect MIME results."""
    config.validate()
    if not isinstance(function_arn, str) or not re.fullmatch(
        r"arn:aws:lambda:eu-north-1:[0-9]{12}:function:incoming_email_handler", function_arn,
    ) or invoked_function_arn != function_arn:
        raise EmailIngressRejected()
    try:
        records = event["Records"]
        if not isinstance(records, list) or len(records) != 1:
            raise EmailIngressRejected()
        record = records[0]
        if record.get("eventSource") != "aws:ses" or record.get("eventVersion") != "1.0" or "s3" in record:
            raise EmailIngressRejected()
        mail, receipt = record["ses"]["mail"], record["ses"]["receipt"]
        if mail.get("headersTruncated") is not False:
            raise EmailIngressRejected()
        action = receipt["action"]
        if action != {"type": "Lambda", "functionArn": function_arn, "invocationType": "Event"}:
            raise EmailIngressRejected()
        if mail.get("destination") != [config.recipient] or receipt.get("recipients") != [config.recipient]:
            raise EmailIngressRejected()
        if any(receipt.get(name, {}).get("status") != "PASS"
               for name in ("dmarcVerdict", "spamVerdict", "virusVerdict")):
            raise EmailIngressRejected()
        # DMARC PASS is the aligned-From proof. An unused SPF/DKIM mechanism can
        # fail legitimately; it cannot substitute for DMARC. Both must be present
        # and at least one PASS for a coherent SES verdict set.
        mechanisms = [receipt.get(name, {}).get("status") for name in ("spfVerdict", "dkimVerdict")]
        if any(status not in ("PASS", "FAIL", "GRAY", "PROCESSING_FAILED") for status in mechanisms) or "PASS" not in mechanisms:
            raise EmailIngressRejected()
        message_id = mail["messageId"]
        if not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id):
            raise EmailIngressRejected()
        received_at = _timestamp(mail["timestamp"])
        triggered_at = _timestamp(receipt["timestamp"])
        if triggered_at < received_at:
            raise EmailIngressRejected()
        require_fresh_receipt(received_at, at_ms=at_ms)
        require_fresh_receipt(triggered_at, at_ms=at_ms)
        headers = mail["headers"]
        if not isinstance(headers, list) or not 1 <= len(headers) <= 1000:
            raise EmailIngressRejected()
        from_headers = [h["value"] for h in headers if isinstance(h, dict) and h.get("name", "").casefold() == "from"]
        if len(from_headers) != 1:
            raise EmailIngressRejected()
        sender = single_from_header(from_headers[0])
        common_from = mail["commonHeaders"]["from"]
        if not isinstance(common_from, list) or len(common_from) != 1 or single_from_header(common_from[0]) != sender:
            raise EmailIngressRejected()
    except Exception:
        raise EmailIngressRejected() from None
    return {
        "version": 1, "source": "ses_direct", "message_id": message_id,
        "received_at_ms": received_at, "sender": sender, "recipient": config.recipient,
        "dmarc": "PASS", "spam": "PASS", "virus": "PASS", "headers_truncated": False,
    }


def bind_object(proof: dict, data: bytes, etag: str, version_id: str | None) -> dict:
    if mime_sender(data) != proof["sender"]:
        raise EmailIngressRejected()
    result = dict(proof, content_sha256=hashlib.sha256(data).hexdigest(), etag=etag, version_id=version_id)
    _validate_object_fields(result)
    return result


def _validate_object_fields(proof: dict) -> None:
    if not isinstance(proof.get("content_sha256"), str) or not SHA256.fullmatch(proof["content_sha256"]):
        raise EmailIngressRejected()
    if not isinstance(proof.get("etag"), str) or not ETAG.fullmatch(proof["etag"]):
        raise EmailIngressRejected()
    version = proof.get("version_id")
    if version is not None and (not isinstance(version, str) or not 1 <= len(version) <= 1024
                                or version == "null" or any(ord(c) < 33 or ord(c) > 126 for c in version)):
        raise EmailIngressRejected()


def validate_notification(notification: dict, config: EmailIngressConfig, *, at_ms: int | None = None) -> None:
    config.validate()
    try:
        if set(notification) != {"bucket", "key", "receipt"}:
            raise EmailIngressRejected()
        proof = notification["receipt"]
        if not isinstance(proof, dict) or set(proof) != PROOF_FIELDS:
            raise EmailIngressRejected()
        if type(proof["version"]) is not int or proof["version"] != 1 or proof["source"] != "ses_direct":
            raise EmailIngressRejected()
        if proof["headers_truncated"] is not False or any(proof[k] != "PASS" for k in ("dmarc", "spam", "virus")):
            raise EmailIngressRejected()
        if proof["sender"] != normalized_address(proof["sender"]) or proof["recipient"] != config.recipient:
            raise EmailIngressRejected()
        if not isinstance(proof["message_id"], str) or not MESSAGE_ID.fullmatch(proof["message_id"]):
            raise EmailIngressRejected()
        if notification["bucket"] != config.bucket or notification["key"] != config.key_prefix + proof["message_id"]:
            raise EmailIngressRejected()
        require_fresh_receipt(proof["received_at_ms"], at_ms=at_ms)
        _validate_object_fields(proof)
    except Exception:
        raise EmailIngressRejected() from None


def verify_object_bytes(notification: dict, data: bytes) -> str:
    proof = notification["receipt"]
    if hashlib.sha256(data).hexdigest() != proof["content_sha256"] or mime_sender(data) != proof["sender"]:
        raise EmailIngressRejected()
    return proof["sender"]


def receipt_digest(notification: dict) -> str:
    # Domain-separated receipt identity; no raw S3 key/message ID is retained.
    return hashlib.sha256(("sofly-ses-v1\n" + notification["bucket"] + "\n" + notification["key"]).encode()).hexdigest()


def proof_digest(notification: dict) -> str:
    return hashlib.sha256(json.dumps(notification, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def owner_binding(user_id: str, grant_id: str) -> str:
    return hashlib.sha256(("sofly-email-owner-v1\n" + user_id + "\n" + grant_id).encode()).hexdigest()
