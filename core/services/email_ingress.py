"""Message-bound, at-most-once email processing with a fresh grant at each send."""

from dataclasses import dataclass

from sqlmodel import Session, select, text, update

from ..config import settings
from ..email_ingress_contract import (
    EmailIngressConfig, EmailIngressRejected, now_ms, owner_binding, proof_digest,
    receipt_digest, validate_notification,
)
from ..models.ai_consent import UserAIConsent, UserAIEmailIdentity, UserAIEmailReceipt
from ..models.email import S3EmailNotification
from ..models.user import User
from .ai_consent import AIConsentRequired, AIConsentUnavailable, read_ai_consent


@dataclass(frozen=True, repr=False)
class EmailEgressContext:
    notification: S3EmailNotification
    user_id: str
    grant_id: str
    receipt_digest: str
    proof_digest: str
    owner_binding: str


def configured_ingress() -> EmailIngressConfig:
    return EmailIngressConfig(
        bucket=settings.FORWARDED_EMAIL_BUCKET,
        key_prefix=settings.FORWARDED_EMAIL_KEY_PREFIX,
        recipient=settings.FORWARDED_EMAIL_RECIPIENT,
    )


def validate_intake(notification: S3EmailNotification) -> dict:
    payload = notification.model_dump(mode="json")
    validate_notification(payload, configured_ingress())
    return payload


def _sender_owner(session: Session, sender: str) -> str | None:
    owners = session.exec(select(User.id).join(UserAIEmailIdentity).where(
        UserAIEmailIdentity.verified_email == sender,
        UserAIEmailIdentity.apple_id == User.apple_id,
        User.verified.is_(True),
    ).limit(2)).all()
    return owners[0] if len(owners) == 1 else None


def _current_grant(session: Session, user_id: str, sender: str, received_at_ms: int) -> str:
    consent = read_ai_consent(session, user_id)
    record = session.get(UserAIConsent, user_id, populate_existing=True)
    if (not record or not consent.forwarded_email_enabled
            or _sender_owner(session, sender) != user_id
            or not record.forwarded_email_grant_id
            or type(record.forwarded_email_granted_at_ms) is not int
            or not 0 <= record.forwarded_email_granted_at_ms < received_at_ms):
        raise AIConsentRequired("forwarded_email")
    return record.forwarded_email_grant_id


def claim_email_receipt(notification: S3EmailNotification) -> EmailEgressContext | None:
    """No queue/replay may grant authority retroactively. One durable claim wins.

    Claims are never deleted or reclaimed automatically. A worker crash after
    claim may lose an import; retrying an external SDK send cannot be made exactly
    once. Safe operator repair requires a new authorized receipt, not old replay.
    """
    payload = validate_intake(notification)
    identity, digest = receipt_digest(payload), proof_digest(payload)
    from ..models import engine

    try:
        with Session(engine) as session:
            session.exec(text("BEGIN IMMEDIATE"))
            previous = session.get(UserAIEmailReceipt, identity)
            if previous is not None:
                if previous.proof_digest != digest:
                    raise EmailIngressRejected()
                return None
            sender, received = notification.receipt.sender, notification.receipt.received_at_ms
            user_id = _sender_owner(session, sender)
            if user_id is None:
                raise AIConsentRequired("forwarded_email")
            grant_id = _current_grant(session, user_id, sender, received)
            binding = owner_binding(user_id, grant_id)
            session.add(UserAIEmailReceipt(
                receipt_digest=identity, proof_digest=digest, owner_binding=binding,
                received_at_ms=received, claimed_at_ms=now_ms(),
            ))
            session.commit()
            return EmailEgressContext(notification, user_id, grant_id, identity, digest, binding)
    except (AIConsentRequired, EmailIngressRejected):
        raise
    except Exception:
        raise AIConsentUnavailable("Email consent unavailable") from None


def require_email_consent_for_send(context: EmailEgressContext | None) -> None:
    if not isinstance(context, EmailEgressContext):
        raise AIConsentRequired("forwarded_email")
    try:
        payload = validate_intake(context.notification)
        if (receipt_digest(payload) != context.receipt_digest
                or proof_digest(payload) != context.proof_digest
                or owner_binding(context.user_id, context.grant_id) != context.owner_binding):
            raise AIConsentRequired("forwarded_email")
    except EmailIngressRejected:
        raise AIConsentRequired("forwarded_email") from None
    from ..models import engine

    try:
        with Session(engine) as session:
            session.exec(text("BEGIN"))
            row = session.get(UserAIEmailReceipt, context.receipt_digest)
            if (not row or row.state != "processing" or row.proof_digest != context.proof_digest
                    or row.owner_binding != context.owner_binding
                    or row.received_at_ms != context.notification.receipt.received_at_ms):
                raise AIConsentRequired("forwarded_email")
            grant_id = _current_grant(session, context.user_id, context.notification.receipt.sender,
                                      context.notification.receipt.received_at_ms)
            if grant_id != context.grant_id:
                raise AIConsentRequired("forwarded_email")
    except AIConsentRequired:
        raise
    except Exception:
        raise AIConsentUnavailable("Email consent unavailable") from None


def finish_email_receipt(context: EmailEgressContext, state: str) -> None:
    if state not in ("completed", "failed", "not_authorized", "no_result"):
        raise ValueError("Invalid email result state")
    from ..models import engine
    try:
        with Session(engine) as session:
            session.exec(update(UserAIEmailReceipt).where(
                UserAIEmailReceipt.receipt_digest == context.receipt_digest,
                UserAIEmailReceipt.proof_digest == context.proof_digest,
                UserAIEmailReceipt.owner_binding == context.owner_binding,
                UserAIEmailReceipt.state == "processing",
            ).values(state=state, finished_at_ms=now_ms()))
            session.commit()
    except Exception:
        raise AIConsentUnavailable("Email receipt store unavailable") from None
