"""New full-journey enrollment API; old experiment routes keep their meaning."""

from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session

from ..activation_journey_contract import ActivationJourneyAssignmentRequest, ActivationJourneyEnrollmentRequest
from ..dependency import get_current_user, check_lambda_auth_token
from ..models import get_session
from ..models.user import User
from ..services.activation_journey import assign_journey, enroll_journey
from ..services.activation_journey_reporting import journey_summary, baseline_manifest

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
    try:
        row = enroll_journey(session=session, user=user, context=data.journey)
        session.commit()
        return {"detail": "success", "measurement_revision": 1, "enrollment_event_id": row.enrollment_event_id}
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise HTTPException(status_code=503, detail="Journey enrollment unavailable") from None
