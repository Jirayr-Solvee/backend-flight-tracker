import json
import logging
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Any, Dict

import boto3
import fitz
import jwt
import requests
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt.algorithms import RSAAlgorithm
from mypy_boto3_s3 import S3Client

from .config import settings
from .models.email import EmailRead
from .models.user import User

logger = logging.getLogger(__name__)


def has_passed(time_str: str) -> bool:
    """
    Check if a UTC datetime is in the past.
    """
    dt = datetime.strptime(time_str, "%Y-%m-%d %H:%MZ").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    return dt < now


def get_s3_client() -> S3Client:
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION,
    )


def parse_email(raw_bytes: bytes) -> EmailRead:
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)

    raw_from = msg.get("From")
    if not raw_from:
        raise ValueError("raw_from not found while parsing email")

    sender = parseaddr(raw_from)[1]
    if not sender:
        raise ValueError(f"unable to retrive sender from raw_from={raw_from}")

    collected_text = []

    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()

        # 1️⃣ Normal body text
        if content_type == "text/plain" and disposition != "attachment":
            try:
                collected_text.append(part.get_content())
            except Exception:
                pass

        # 2️⃣ Forwarded email (nested message)
        elif content_type == "message/rfc822":
            try:
                nested = part.get_payload(0)
                if nested:
                    for nested_part in nested.walk():
                        if nested_part.get_content_type() == "text/plain":
                            collected_text.append(nested_part.get_content())
            except Exception:
                pass

        # 3️⃣ PDF attachment
        elif content_type == "application/pdf":
            try:
                pdf_bytes = part.get_payload(decode=True)
                if pdf_bytes:
                    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                    for page in doc:
                        collected_text.append(page.get_text())
            except Exception:
                # Corrupted or unreadable PDF
                continue

    # 🔥 Merge everything cleanly
    full_body = "\n\n".join(collected_text).strip()

    return EmailRead(
        sender=sender,
        body=full_body,
    )


def create_jwt(
    *,
    sub: str,
    extra_claims: Dict[str, Any] | None = None,
) -> str:

    now = datetime.now(tz=timezone.utc)
    exp = now + timedelta(days=settings.JWT_EXPIRE_DAYS)

    payload: Dict[str, Any] = {
        "sub": sub,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }

    if extra_claims:
        payload.update(extra_claims)

    token = jwt.encode(
        payload,
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )

    return token


def decode_jwt(token: str) -> Dict[str, Any]:
    payload = jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
    )
    return payload


def user_has_active_subscription(user: User) -> bool:
    if not user.has_searched:
        return True

    if user.premium_valid_until is None:
        return False

    now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)

    return user.premium_valid_until > now_ts


def calculate_premium_valid_until(
    transaction_expiration_ms: int | None,
) -> int | None:
    if not transaction_expiration_ms:
        return None

    now = datetime.now(timezone.utc)

    transaction_expiration = datetime.fromtimestamp(
        transaction_expiration_ms / 1000,
        tz=timezone.utc,
    )

    if transaction_expiration <= now:
        return None

    max_allowed = now + timedelta(hours=settings.MAX_PREMIUM_HOURS)

    final_expiration = min(transaction_expiration, max_allowed)

    return int(final_expiration.timestamp() * 1000)


def verify_apple_identity_token(identity_token: str) -> dict:
    # 1️⃣ Get Apple public keys
    apple_keys = requests.get(settings.APPLE_KEYS_URL).json()["keys"]

    # 2️⃣ Decode header to find the correct key
    header = jwt.get_unverified_header(identity_token)
    key_dict = next(k for k in apple_keys if k["kid"] == header["kid"])

    # 3️⃣ Build public key (JSON string ensures PyJWT type checks correctly)
    public_key: RSAPublicKey = RSAAlgorithm.from_jwk(json.dumps(key_dict))

    # 4️⃣ Verify token
    decoded = jwt.decode(
        identity_token,
        key=public_key,
        algorithms=["RS256"],
        audience=settings.BUNDLE_ID,
        issuer=settings.APPLE_ISSUER,
    )

    return decoded


def get_time(time_obj, attr: str) -> str | None:
    return getattr(time_obj, attr) if time_obj else None
