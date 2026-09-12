from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from ..dependency import get_current_user
from ..models import get_session
from ..models.user import User
from ..security_logging import CredentialSafeRoute
from ..services.ai_consent import (
    AIConsentAccountUnavailable, AIConsentConflict, AIConsentEmailVerificationRequired,
    AIConsentRead, AIConsentUpdate, read_ai_consent, update_ai_consent,
    record_verified_apple_email_identity, verified_email_from_apple_claims,
)
from ..utils import verify_apple_identity_token

router = APIRouter(route_class=CredentialSafeRoute)


@router.get("/me/ai-consent", response_model=AIConsentRead)
def get_consent(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    return read_ai_consent(session, user.id)


@router.put("/me/ai-consent", response_model=AIConsentRead)
def put_consent(
    data: AIConsentUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    # Required frozen owner protects against torn/stale account-token snapshots.
    if data.user_id != user.id:
        raise HTTPException(409, {"code": "ai_consent_account_mismatch"})
    try:
        return update_ai_consent(session, data)
    except AIConsentAccountUnavailable:
        raise HTTPException(401, "Authorization required") from None
    except AIConsentEmailVerificationRequired:
        raise HTTPException(409, {"code": "ai_consent_email_verification_required"}) from None
    except AIConsentConflict as error:
        raise HTTPException(409, {
            "code": "ai_consent_conflict", "consent": error.consent.model_dump(),
        }) from None


class AIEmailVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=128)
    apple_jwt: str = Field(min_length=1, max_length=16384, repr=False)


@router.post("/me/ai-consent/verify-email", response_model=AIConsentRead)
async def verify_email(
    data: AIEmailVerificationRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if data.user_id != user.id:
        raise HTTPException(409, {"code": "ai_consent_account_mismatch"})
    if not user.verified or not user.apple_id:
        raise HTTPException(409, {"code": "ai_consent_sign_in_required"})
    owner_id, apple_id = user.id, user.apple_id
    # Server-side capture requires no frontend wire change. Never let an older
    # suspended verification replace proof or revoke a newer explicit grant.
    captured_revision = read_ai_consent(session, owner_id).revision
    try:
        claims = await verify_apple_identity_token(data.apple_jwt)
    except Exception:
        # SDK/network/token errors can echo the private JWT. No exception chain.
        raise HTTPException(401, {"code": "ai_consent_identity_verification_failed"}) from None
    if claims.get("sub") != apple_id:
        raise HTTPException(409, {"code": "ai_consent_account_mismatch"})
    if not verified_email_from_apple_claims(claims):
        raise HTTPException(409, {"code": "ai_consent_email_verification_required"})
    try:
        record_verified_apple_email_identity(
            session, owner_id, claims, expected_revision=captured_revision,
        )
    except AIConsentAccountUnavailable:
        raise HTTPException(409, {"code": "ai_consent_account_mismatch"}) from None
    except AIConsentConflict as error:
        raise HTTPException(409, {
            "code": "ai_consent_conflict", "consent": error.consent.model_dump(),
        }) from None
    session.commit()
    return read_ai_consent(session, owner_id)
