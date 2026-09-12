"""Consent is checked from a fresh DB session at every Gemini send, not cached."""

import time
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, delete, select, text, update

from ..models.ai_consent import AIConsentReceipt, UserAIConsent, UserAIEmailIdentity
from ..models.user import User

POLICY_VERSION = 1
AIConsentPurpose = Literal["search", "forwarded_email"]


class AIConsentRead(BaseModel):
    user_id: str
    policy_version: int = POLICY_VERSION
    revision: int = 0
    search_enabled: bool = False
    forwarded_email_enabled: bool = False
    forwarded_email_verified: bool = False
    updated_at: int | None = None


class AIConsentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    policy_version: Literal[1]
    expected_revision: int = Field(strict=True, ge=0)
    purpose: AIConsentPurpose
    enabled: bool = Field(strict=True)
    request_id: UUID


class AIConsentRequired(Exception):
    def __init__(self, purpose: AIConsentPurpose):
        self.purpose = purpose
        super().__init__("AI consent required")


class AIConsentUnavailable(Exception):
    pass


class AIConsentAccountUnavailable(Exception):
    pass


class AIConsentEmailVerificationRequired(Exception):
    pass


class AIConsentConflict(Exception):
    def __init__(self, consent: AIConsentRead):
        self.consent = consent
        super().__init__("AI consent revision conflict")


def read_ai_consent(session: Session, user_id: str) -> AIConsentRead:
    identity = session.exec(select(UserAIEmailIdentity).join(User).where(
        UserAIEmailIdentity.user_id == user_id,
        UserAIEmailIdentity.apple_id == User.apple_id,
        User.verified.is_(True),
    ).execution_options(populate_existing=True)).first()
    email_verified = identity is not None
    record = session.exec(
        select(UserAIConsent).where(UserAIConsent.user_id == user_id)
        .execution_options(populate_existing=True)
    ).first()
    if record is None:
        return AIConsentRead(user_id=user_id, forwarded_email_verified=email_verified)
    current_policy = record.policy_version == POLICY_VERSION
    return AIConsentRead(
        user_id=user_id,
        revision=record.revision,
        search_enabled=current_policy and record.search_enabled,
        forwarded_email_enabled=(current_policy and email_verified and record.forwarded_email_enabled
                                 and bool(record.forwarded_email_grant_id)
                                 and record.forwarded_email_granted_at_ms is not None),
        forwarded_email_verified=email_verified,
        updated_at=record.updated_at,
    )


def update_ai_consent(session: Session, data: AIConsentUpdate) -> AIConsentRead:
    # A no-op write obtains SQLite's writer lock even when no row exists yet.
    # Check account existence under that lock BEFORE inserting a foreign-key
    # row. Independent workers serialize; revision remains the acceptance gate.
    session.exec(update(UserAIConsent).where(UserAIConsent.user_id == data.user_id).values(
        revision=UserAIConsent.revision,
    ))
    if session.exec(select(User.id).where(User.id == data.user_id)).first() is None:
        session.rollback()
        raise AIConsentAccountUnavailable("Authorization required")
    session.exec(insert(UserAIConsent).values(user_id=data.user_id).on_conflict_do_nothing(
        index_elements=["user_id"],
    ))
    receipt_key = (data.user_id, str(data.request_id))
    receipt = session.get(AIConsentReceipt, receipt_key)
    current = read_ai_consent(session, data.user_id)
    command = {
        "policy_version": data.policy_version,
        "expected_revision": data.expected_revision,
        "purpose": data.purpose,
        "enabled": data.enabled,
    }
    if receipt is not None:
        if all(getattr(receipt, key) == value for key, value in command.items()):
            session.commit()
            # Never replay an old enabled snapshot after a later revocation.
            return current
        session.rollback()
        raise AIConsentConflict(current)
    if current.revision != data.expected_revision:
        session.rollback()
        raise AIConsentConflict(current)
    if data.purpose == "forwarded_email" and data.enabled and not current.forwarded_email_verified:
        session.rollback()
        raise AIConsentEmailVerificationRequired("Verified Apple email required")

    values = {
        "policy_version": POLICY_VERSION,
        "revision": current.revision + 1,
        "updated_at": int(time.time() * 1000),
        "search_enabled": current.search_enabled,
        "forwarded_email_enabled": current.forwarded_email_enabled,
        f"{data.purpose}_enabled": data.enabled,
    }
    if data.purpose == "forwarded_email":
        values["forwarded_email_grant_id"] = str(uuid4()) if data.enabled else None
        values["forwarded_email_granted_at_ms"] = int(time.time() * 1000) if data.enabled else None
    result = session.exec(update(UserAIConsent).where(
        UserAIConsent.user_id == data.user_id,
        UserAIConsent.revision == data.expected_revision,
    ).values(**values))
    if result.rowcount != 1:
        session.rollback()
        raise AIConsentConflict(read_ai_consent(session, data.user_id))
    session.add(AIConsentReceipt(user_id=data.user_id, request_id=str(data.request_id), **command))
    session.commit()
    return read_ai_consent(session, data.user_id)


def verified_email_from_apple_claims(claims: dict) -> str | None:
    raw_email = claims.get("email")
    verified = claims.get("email_verified")
    email = raw_email.strip().casefold() if isinstance(raw_email, str) else ""
    valid = (verified is True or verified == "true") and (
        3 <= len(email) <= 320 and email.count("@") == 1 and not any(c.isspace() for c in email)
    )
    return email if valid else None


def record_verified_apple_email_identity(
    session: Session, user_id: str, claims: dict,
    *, expected_revision: int | None = None,
) -> None:
    """Only call after signature verification. Recheck subject under write lock.

    Changing the proven sender revokes its old email grant and advances revision,
    so a delayed Allow for the previous sender cannot apply to the new identity.
    """
    email = verified_email_from_apple_claims(claims)
    session.exec(update(UserAIConsent).where(UserAIConsent.user_id == user_id).values(
        revision=UserAIConsent.revision,
    ))
    user = session.get(User, user_id, populate_existing=True)
    if not user or not user.verified or not user.apple_id or user.apple_id != claims.get("sub"):
        session.rollback()
        raise AIConsentAccountUnavailable("Sign-in account does not match")
    session.exec(insert(UserAIConsent).values(user_id=user_id).on_conflict_do_nothing(
        index_elements=["user_id"],
    ))
    current = read_ai_consent(session, user_id)
    if expected_revision is not None and current.revision != expected_revision:
        session.rollback()
        raise AIConsentConflict(current)
    previous = session.get(UserAIEmailIdentity, user_id, populate_existing=True)
    if previous and email and previous.verified_email == email and previous.apple_id == claims.get("sub"):
        return
    if previous:
        session.exec(delete(UserAIEmailIdentity).where(UserAIEmailIdentity.user_id == user_id))
    if email:
        session.add(UserAIEmailIdentity(user_id=user_id, apple_id=claims["sub"], verified_email=email))
    if previous or email:
        session.exec(update(UserAIConsent).where(UserAIConsent.user_id == user_id).values(
            forwarded_email_enabled=False,
            forwarded_email_grant_id=None,
            forwarded_email_granted_at_ms=None,
            revision=UserAIConsent.revision + 1,
            updated_at=int(time.time() * 1000),
        ))


def require_ai_consent(
    user_id: str | None, purpose: AIConsentPurpose, *, expected_sender: str | None = None,
) -> None:
    if not user_id or purpose not in ("search", "forwarded_email"):
        raise AIConsentRequired(purpose)
    # Import lazily to avoid the model-package initialization cycle. The short
    # independent transaction also avoids a request/worker's stale identity map.
    from ..models import engine

    try:
        with Session(engine) as session:
            # Python SQLite legacy transaction mode does not begin on SELECT.
            # Explicit BEGIN gives the account, grant, proof and uniqueness
            # checks one current snapshot; no long request session is reused.
            session.exec(text("BEGIN"))
            user_exists = session.get(User, user_id) is not None
            consent = read_ai_consent(session, user_id)
            enabled = getattr(consent, f"{purpose}_enabled")
            if purpose == "forwarded_email" and expected_sender is not None:
                matches = session.exec(select(UserAIEmailIdentity.user_id).join(User).where(
                    UserAIEmailIdentity.verified_email == expected_sender,
                    UserAIEmailIdentity.apple_id == User.apple_id,
                    User.verified.is_(True),
                ).limit(2)).all()
                enabled = enabled and matches == [user_id]
    except Exception:
        # A storage outage must never turn into permission or leak SQL values.
        raise AIConsentUnavailable("AI consent unavailable") from None
    if not user_exists or not enabled:
        raise AIConsentRequired(purpose)
