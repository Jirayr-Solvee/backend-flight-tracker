"""New full-journey enrollment API; old experiment routes keep their meaning."""

from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session

from ..activation_journey_contract import (
    ActivationJourneyAssignmentRequest, ActivationJourneyEnrollmentRequest, CLOSABLE_FLOW_ID,
)
from ..dependency import get_current_user, check_lambda_auth_token
from ..models import get_session
from ..models.user import User
from ..services.activation_journey import assign_closable_flow, assign_journey, enroll_journey
from ..services.activation_journey_reporting import journey_summary, baseline_manifest
from ..services.closable_flow_reporting import closable_flow_summary

router = APIRouter()


@router.get("/activation-journey/summary", dependencies=[Depends(check_lambda_auth_token)])
def summary(analytics_environment: Literal["production", "development", "testflight"] = "production",
            since_ms: int | None = Query(default=None, ge=0), until_ms: int | None = Query(default=None, ge=0),
            as_of_ms: int | None = Query(default=None, ge=0), app_version: str | None = Query(default=None, max_length=40),
            build_number: str | None = Query(default=None, max_length=40), session: Session = Depends(get_session)):
    return journey_summary(session=session, analytics_environment=analytics_environment, since_ms=since_ms,
                           until_ms=until_ms, as_of_ms=as_of_ms, app_version=app_version, build_number=build_number)


@router.get("/activation-journey/baseline-manifest", dependencies=[Depends(check_lambda_auth_token)])
def historical_baseline(as_of_ms: int = Query(ge=0), analytics_environment: Literal["production", "development", "testflight"] = "production",
                        after: str | None = Query(default=None, max_length=250), limit: int = Query(default=500, ge=1, le=2000),
                        session: Session = Depends(get_session)):
    return baseline_manifest(session=session, analytics_environment=analytics_environment, as_of_ms=as_of_ms, after=after, limit=limit)


@router.post("/activation-journey/assignment")
def assignment(data: ActivationJourneyAssignmentRequest, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    try:
        result = assign_journey(session=session, user=user, data=data)
        session.commit()
        return result
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise HTTPException(status_code=503, detail="Journey assignment unavailable") from None


@router.post("/activation-journey/enrollment")
def enrollment(data: ActivationJourneyEnrollmentRequest, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if data.journey.experiment_id == CLOSABLE_FLOW_ID:
        # Its only enrollment trigger is the canonical exposure diagnostic, so a
        # randomized enrollment always means the exposure arrived.
        raise HTTPException(status_code=422, detail="Closable-flow enrollment uses its canonical exposure event")
    try:
        row = enroll_journey(session=session, user=user, context=data.journey)
        session.commit()
        return {"detail": "success", "measurement_revision": row.measurement_revision, "enrollment_event_id": row.enrollment_event_id}
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise HTTPException(status_code=503, detail="Journey enrollment unavailable") from None


@router.post("/closable-flow/assignment")
def closable_flow_assignment(data: ActivationJourneyAssignmentRequest, user: User = Depends(get_current_user),
                             session: Session = Depends(get_session)):
    """Assignment and the operational refresh: the frozen context plus current switches."""
    try:
        result = assign_closable_flow(session=session, user=user, data=data)
        session.commit()
        return result
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise HTTPException(status_code=503, detail="Closable-flow assignment unavailable") from None


@router.get("/closable-flow/summary", dependencies=[Depends(check_lambda_auth_token)])
def closable_flow_report(analytics_environment: Literal["production", "development", "testflight"] = "production",
                         measurement_revision: int = Query(ge=1, le=100),
                         since_ms: int | None = Query(default=None, ge=0), until_ms: int | None = Query(default=None, ge=0),
                         as_of_ms: int | None = Query(default=None, ge=0), app_version: str | None = Query(default=None, max_length=40),
                         build_number: str | None = Query(default=None, max_length=40), session: Session = Depends(get_session)):
    # One revision per read: revisions are never pooled.
    return closable_flow_summary(session=session, analytics_environment=analytics_environment,
                                 measurement_revision=measurement_revision, since_ms=since_ms, until_ms=until_ms,
                                 as_of_ms=as_of_ms, app_version=app_version, build_number=build_number)
