import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from ..dependency import get_current_user
from ..models import get_session
from ..models.flight import Flight
from ..models.flight_share import FlightShare, current_time_ms
from ..models.user import User
from ..security_logging import (
    CredentialOperation, CredentialSafeRoute, rollback_and_log_failure,
)
from ..services import flight_share as share_service
from ..services.flight_share_page import (
    PUBLIC_HEADERS, is_bot, is_prefetch, pick_language, render_active_page, render_unavailable_page,
)

# Owner endpoints, mounted under /flights.
router = APIRouter(route_class=CredentialSafeRoute)
# Anonymous viewer endpoints, mounted under /public/share. nginx maps
# https://sofly.to/f/<token>[/live.json] here and must not log the token.
public_router = APIRouter(route_class=CredentialSafeRoute)

logger = logging.getLogger(__name__)

FlightID = Annotated[int, Path(ge=1, le=9_223_372_036_854_775_807)]
_UNAVAILABLE_STATUS = {"not_found": 404, "expired": 410}


class FlightShareLinkRead(BaseModel):
    # The URL is the bearer secret for the public page: keep it out of reprs.
    url: str = Field(repr=False)
    expires_at_utc: str
    created: bool


def _link(share: FlightShare, flight: Flight, created: bool) -> FlightShareLinkRead:
    return FlightShareLinkRead(
        url=share_service.share_url(share),
        expires_at_utc=share_service.utc_text(share_service.share_expires_at(flight)),
        created=created,
    )


@router.post("/{flight_id}/share", response_model=FlightShareLinkRead)
def create_flight_share(
    flight_id: FlightID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    owner_id = user.id
    try:
        flight = session.get(Flight, flight_id)
        if flight is None or not share_service.owns_flight(session, owner_id, flight_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flight not found")
        if share_service.share_expires_at(flight) <= share_service.utc_now():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="flight_ended")
        share, created = share_service.create_or_reuse_share(session, owner_id, flight_id)
        response = _link(share, flight, created)
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        rollback_and_log_failure(session, logger, CredentialOperation.FLIGHT_SHARE_CREATE)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
    logger.info("flight_share_create outcome=%s", "created" if created else "reused")
    return response


@router.get("/{flight_id}/share", response_model=FlightShareLinkRead)
def get_flight_share(
    flight_id: FlightID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    share = share_service.find_active_share(session, user.id, flight_id)
    flight = session.get(Flight, flight_id) if share is not None else None
    if share is None or flight is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")
    try:
        # Same URL as POST would return, so it resolves after a secret rotation.
        share = share_service.rekey_if_needed(session, share)
        response = _link(share, flight, False)
    except Exception:
        rollback_and_log_failure(session, logger, CredentialOperation.FLIGHT_SHARE_CREATE)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
    return response


@router.delete("/{flight_id}/share", response_model=dict)
def revoke_flight_share(
    flight_id: FlightID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    owner_id = user.id
    try:
        owns_flight = share_service.owns_flight(session, owner_id, flight_id)
        revoked = share_service.revoke_flight_shares(session, owner_id, flight_id, "owner")
        if not owns_flight and not revoked:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flight not found")
        session.commit()
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        rollback_and_log_failure(session, logger, CredentialOperation.FLIGHT_SHARE_REVOKE)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
    logger.info("flight_share_revoke outcome=%s", "revoked" if revoked else "not_active")
    return {"revoked": revoked}


def _resolve(session: Session, token: str) -> tuple[str, FlightShare | None, dict | None]:
    # Database rows only: a public read must never reach a paid provider.
    now = share_service.utc_now()
    state, share, flight, expires_at = share_service.resolve_public_share(session, token, now)
    if state != "active":
        return state, None, None
    return state, share, share_service.build_live_state(flight, now=now, expires_at=expires_at)


@public_router.api_route("/{token}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def public_share_page(token: str, request: Request, session: Session = Depends(get_session)):
    language = pick_language(request.headers.get("accept-language"))
    try:
        state, share, live_state = _resolve(session, token)
        page = (
            render_active_page(live_state, language) if state == "active"
            else render_unavailable_page(state, language)
        )
    except Exception:
        rollback_and_log_failure(session, logger, CredentialOperation.FLIGHT_SHARE_READ)
        return HTMLResponse(
            render_unavailable_page("error", language), status_code=500, headers=PUBLIC_HEADERS,
        )
    logger.info("flight_share_read format=html outcome=%s", state)
    if state != "active":
        return HTMLResponse(page, status_code=_UNAVAILABLE_STATUS[state], headers=PUBLIC_HEADERS)
    # Aggregate count of people opening the page: no HEAD, polls or previews.
    if (
        request.method == "GET"
        and not is_bot(request.headers.get("user-agent"))
        and not is_prefetch(request.headers)
    ):
        try:
            share_service.record_view(session, share.id, current_time_ms())
        except Exception:
            rollback_and_log_failure(session, logger, CredentialOperation.FLIGHT_SHARE_READ)
    return HTMLResponse(page, headers=PUBLIC_HEADERS)


@public_router.api_route("/{token}/live.json", methods=["GET", "HEAD"])
def public_share_live_state(token: str, session: Session = Depends(get_session)):
    try:
        state, _, live_state = _resolve(session, token)
    except Exception:
        rollback_and_log_failure(session, logger, CredentialOperation.FLIGHT_SHARE_READ)
        return JSONResponse({"state": "error"}, status_code=500, headers=PUBLIC_HEADERS)
    if state != "active":
        # Active polls are routine (every 30 s per viewer) and stay unlogged.
        logger.info("flight_share_read format=json outcome=%s", state)
        return JSONResponse({"state": state}, status_code=_UNAVAILABLE_STATUS[state], headers=PUBLIC_HEADERS)
    return JSONResponse(live_state, headers=PUBLIC_HEADERS)
