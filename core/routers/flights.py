import logging
import time
from datetime import date, timedelta

from fastapi import (APIRouter, BackgroundTasks, Depends, Header, HTTPException,
                     Query, status)
from sqlmodel import Session, select

from ..background_tasks import create_webhook_for_flight
from ..dependency import check_lambda_auth_token, get_current_user
from ..models import get_session
from ..models.flight import (
    CopilotTelemetryRead,
    Flight,
    FlightRead,
    GlobalFlightResolveCandidatesRequest,
    GlobalFlightPositionRead,
    QuerySearchResponse,
    SearchDiagnosticsRead,
    SearchFailureReportRequest,
    SearchQueryRequest,
    SearchRecoveryRead,
)
from ..models.live_activity import LiveActivityRegistration
from ..models.user import User, UserFlightLink
from ..services.flight.delay_risk import DelayRiskResponse, DelayRiskService
from ..services.flight import FlightPersistence, FlightService
from ..services.flight.api_client import AerodataboxUnavailableError
from ..services.gemini.service import GeminiService, ResolvedFunctionCall
from ..services.search_failure import RETENTION_DAYS, SearchFailureService
from ..utils import user_has_active_subscription, normalize_offset

router = APIRouter()

logger = logging.getLogger(__name__)


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))


def _log_search_provider(
    *,
    query_type: str,
    outcome: str,
    latency_ms: int,
    result_count: int,
) -> None:
    # Keep search text out of logs. These bounded dimensions are sufficient for
    # provider latency, outage, and rate-limit monitoring.
    logger.info(
        "flight_search_provider query_type=%s outcome=%s latency_ms=%s result_count=%s",
        query_type,
        outcome,
        latency_ms,
        result_count,
    )


def _record_backend_search_failure(
    *,
    session: Session,
    user: User,
    query: str,
    diagnostics: SearchDiagnosticsRead,
    structured_args: dict | None = None,
    app_version: str | None = None,
    build_number: str | None = None,
    analytics_environment: str = "unknown",
) -> None:
    if not diagnostics.failure_reason:
        return

    sample = SearchFailureService.record(
        session=session,
        user_id=user.id,
        query=query,
        source="backend",
        query_type=diagnostics.query_type,
        failure_reason=diagnostics.failure_reason,
        provider_outcome=diagnostics.provider_outcome,
        normalization_applied=diagnostics.normalization_applied,
        provider_result_count=diagnostics.provider_result_count,
        provider_latency_ms=diagnostics.provider_latency_ms,
        structured_args=structured_args,
        app_version=app_version,
        build_number=build_number,
        analytics_environment=analytics_environment,
    )
    diagnostics.failure_sample_id = sample.id


def _search_result_count(response: QuerySearchResponse) -> int:
    return len(response.flights_result) + len(response.airport_flights_result)


def _actionable_search_result_count(response: QuerySearchResponse) -> int:
    landed_statuses = {"arrived", "landed"}

    def is_actionable(status_value) -> bool:
        normalized = str(getattr(status_value, "value", status_value)).casefold()
        return normalized not in landed_statuses

    return sum(
        is_actionable(flight.status)
        for flight in response.flights_result
    ) + sum(
        is_actionable(flight.status)
        for flight in response.airport_flights_result
    )


async def _execute_search_with_date_fallback(
    *,
    resolved_call: ResolvedFunctionCall,
    query: str,
    session: Session,
) -> QuerySearchResponse:
    """Search the requested date, then a bounded upcoming window when ambiguous."""
    initial_args = dict(resolved_call.args)
    initial_response = await resolved_call.handler(
        **initial_args,
        session=session,
    )
    max_days_by_function = {
        "extract_flight_info": 3,
        "extract_flight_info_via_airport": 2,
        "extract_flight_info_via_airport_single_derection": 1,
    }
    max_days = min(
        GeminiService.upcoming_search_days(query),
        max_days_by_function.get(resolved_call.function_name, 0),
    )
    if _search_result_count(initial_response) > 0 and (
        max_days <= 0 or _actionable_search_result_count(initial_response) > 0
    ):
        return initial_response
    if max_days <= 0:
        return initial_response

    try:
        initial_date = date.fromisoformat(str(initial_args["departure_date"]))
    except (KeyError, TypeError, ValueError):
        return initial_response

    for offset in range(1, max_days + 1):
        candidate_args = {
            **initial_args,
            "departure_date": (initial_date + timedelta(days=offset)).isoformat(),
        }
        candidate_response = await resolved_call.handler(
            **candidate_args,
            session=session,
        )
        if _search_result_count(candidate_response) > 0:
            # Persist the date that actually produced the response so protected
            # diagnostics can explain a later client-side filtering failure.
            resolved_call.args = candidate_args
            return candidate_response

    return initial_response


@router.get("/live-positions", response_model=list[GlobalFlightPositionRead])
async def get_global_live_flight_positions(
    limit: int = Query(5000, ge=1, le=20000),
    user: User = Depends(get_current_user),
):
    return await FlightService.get_global_flight_positions(limit=limit)


@router.get("/live-positions/resolve", response_model=FlightRead)
async def resolve_global_live_flight(
    callsign: str = Query(..., min_length=3),
    icao24: str | None = Query(None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        flight = await FlightService.resolve_global_live_flight(
            session=session,
            callsign=callsign,
            icao24=icao24,
        )
        if not flight:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Unable to resolve live flight",
            )

        session.commit()
        return FlightRead.model_validate(flight, from_attributes=True)
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        logger.exception(
            f"Unable to resolve global live flight callsign={callsign}, icao24={icao24}, user id={user.id}"
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post("/live-positions/resolve-candidates", response_model=FlightRead)
async def resolve_global_live_flight_candidates(
    payload: GlobalFlightResolveCandidatesRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        flight = await FlightService.resolve_global_live_flight_candidates(
            session=session,
            candidates=payload.candidates,
        )
        if not flight:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Unable to resolve live flight",
            )

        session.commit()
        return FlightRead.model_validate(flight, from_attributes=True)
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        logger.exception(
            f"Unable to resolve global live flight candidates user id={user.id}"
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.delete("/{flight_id}/delete", response_model=dict)
def delete_flight_for_a_user(
    flight_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        deleted = FlightPersistence.delete_user_flight_link(
            session=session,
            flight_id=flight_id,
            user_id=user.id,
        )

        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Flight not found",
            )

        device_ids = [device.id for device in user.devices]
        if device_ids:
            registrations = session.exec(
                select(LiveActivityRegistration).where(
                    LiveActivityRegistration.flight_id == flight_id,
                    LiveActivityRegistration.device_id.in_(device_ids),  # type: ignore[attr-defined]
                )
            ).all()
            for registration in registrations:
                registration.active = False
                registration.updated_at = int(time.time())
                session.add(registration)

        session.commit()

        return {"detail": "successful"}
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        logger.exception(f"Unable to delete flight id={flight_id}, user id={user.id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.put("/{flight_id}/assign", response_model=dict)
def assign_flight_to_a_user(
    flight_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if not user_has_active_subscription(user=user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not Allowed")

    try:
        flight = session.get(Flight, flight_id)
        if not flight:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Flight not found"
            )

        user_flight_link = FlightPersistence.get_user_flight_link(
            session=session, flight_id=flight_id, user_id=user.id
        )

        if user_flight_link:
            return {"detail": "successful"}

        FlightPersistence.create_user_flight_link(
            session=session, flight_id=flight_id, user_id=user.id
        )

        user.has_searched = True
        session.commit()

        return {"detail": "successful"}
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        logger.exception(f"Unable to assign flight id={flight_id}, user id={user.id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.get("/{flight_id}/delay-risk", response_model=DelayRiskResponse)
async def get_flight_delay_risk(
    flight_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if not user_has_active_subscription(user=user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not Allowed")

    flight = session.get(Flight, flight_id)
    if not flight:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Flight not found"
        )

    link = session.exec(
        select(UserFlightLink).where(
            UserFlightLink.user_id == user.id,
            UserFlightLink.flight_id == flight_id,
        )
    ).first()
    if not link:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Flight not found"
        )

    try:
        return await DelayRiskService.build_delay_risk(flight=flight)
    except Exception:
        logger.exception(
            f"Unable to build delay risk for flight id={flight_id}, user id={user.id}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.get(
    "/{flight_id}/copilot-telemetry",
    response_model=CopilotTelemetryRead,
)
async def get_flight_copilot_telemetry(
    flight_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if not user_has_active_subscription(user=user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not Allowed")

    flight = session.get(Flight, flight_id)
    if not flight:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Flight not found"
        )

    link = session.exec(
        select(UserFlightLink).where(
            UserFlightLink.user_id == user.id,
            UserFlightLink.flight_id == flight_id,
        )
    ).first()
    if not link:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Flight not found"
        )

    return await FlightService.get_copilot_telemetry(flight=flight)


@router.get(
    "/search/term",
    summary="Search for flights using nomral text",
    response_model=QuerySearchResponse,
)
async def search_flights_from_text(
    term: str = Query(..., min_length=2),
    language: str | None = Query(default=None),
    app_version: str | None = Query(default=None, max_length=40),
    build_number: str | None = Query(default=None, max_length=40),
    analytics_environment: str = Query(default="unknown", max_length=20),
    accept_language: str | None = Header(default=None),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    ai_service = GeminiService()
    normalized_term = ai_service.normalize_query(term)
    normalization_applied = normalized_term != term.strip()
    resolved_call = None

    try:
        registration = ai_service.aircraft_registration_from_query(normalized_term)
        if registration:
            provider_started_at = time.perf_counter()
            registration_result = await FlightService.resolve_live_aircraft_registration(
                session=session,
                registration=registration,
            )
            provider_latency_ms = _elapsed_ms(provider_started_at)
            provider_outcome = registration_result.failure_reason or "results"
            _log_search_provider(
                query_type="aircraft_registration",
                outcome=provider_outcome,
                latency_ms=provider_latency_ms,
                result_count=registration_result.provider_result_count,
            )
            diagnostics = SearchDiagnosticsRead(
                query_type="aircraft_registration",
                failure_reason=registration_result.failure_reason,
                normalization_applied=(
                    normalization_applied
                    or registration != normalized_term.upper()
                ),
                provider_result_count=registration_result.provider_result_count,
                provider_outcome=provider_outcome,
                provider_latency_ms=provider_latency_ms,
            )
            if registration_result.flight:
                session.commit()
                return QuerySearchResponse(
                    flights_result=[
                        FlightRead.model_validate(
                            registration_result.flight,
                            from_attributes=True,
                        )
                    ],
                    diagnostics=diagnostics,
                )

            response = QuerySearchResponse(
                recovery=ai_service.registration_recovery(
                    registration=registration,
                    failure_reason=(
                        registration_result.failure_reason
                        or "registration_live_unresolved"
                    ),
                ),
                diagnostics=diagnostics,
            )
            _record_backend_search_failure(
                session=session,
                user=user,
                query=normalized_term,
                diagnostics=diagnostics,
                app_version=app_version,
                build_number=build_number,
                analytics_environment=analytics_environment,
            )
            session.commit()
            return response

        preflight_recovery = ai_service.preflight_recovery(
            normalized_term,
            language=language or accept_language,
        )
        if preflight_recovery:
            diagnostics = SearchDiagnosticsRead(
                query_type=preflight_recovery.detected_query_type,
                failure_reason=ai_service.failure_reason_for_recovery(
                    preflight_recovery
                ),
                normalization_applied=normalization_applied,
                provider_result_count=0,
                provider_outcome="not_called",
            )
            response = QuerySearchResponse(
                recovery=preflight_recovery,
                diagnostics=diagnostics,
            )
            _record_backend_search_failure(
                session=session,
                user=user,
                query=normalized_term,
                diagnostics=diagnostics,
                app_version=app_version,
                build_number=build_number,
                analytics_environment=analytics_environment,
            )
            session.commit()
            return response

        resolved_call = await ai_service.get_function_call(
            query=normalized_term,
            language=language or accept_language,
        )

        if not resolved_call:
            logger.warning("Gemini unable to retrieve a search function call")
            recovery = ai_service.recovery_for_empty_result(
                query=normalized_term,
                resolved_call=None,
            )
            diagnostics = SearchDiagnosticsRead(
                query_type=recovery.detected_query_type,
                failure_reason=ai_service.failure_reason_for_recovery(recovery),
                normalization_applied=normalization_applied,
                provider_result_count=0,
                provider_outcome="not_called",
            )
            response = QuerySearchResponse(
                recovery=recovery,
                diagnostics=diagnostics,
            )
            _record_backend_search_failure(
                session=session,
                user=user,
                query=normalized_term,
                diagnostics=diagnostics,
                app_version=app_version,
                build_number=build_number,
                analytics_environment=analytics_environment,
            )
            session.commit()
            return response

        query_type = ai_service.query_type_for_call(resolved_call)
        provider_started_at = time.perf_counter()
        try:
            flights = await _execute_search_with_date_fallback(
                resolved_call=resolved_call,
                query=normalized_term,
                session=session,
            )
        except AerodataboxUnavailableError as exc:
            provider_latency_ms = _elapsed_ms(provider_started_at)
            failure_reason = (
                "provider_rate_limited" if exc.rate_limited else "provider_unavailable"
            )
            _log_search_provider(
                query_type=query_type,
                outcome=failure_reason,
                latency_ms=provider_latency_ms,
                result_count=0,
            )
            diagnostics = SearchDiagnosticsRead(
                query_type=query_type,
                failure_reason=failure_reason,
                normalization_applied=normalization_applied,
                provider_result_count=0,
                provider_outcome=failure_reason,
                provider_latency_ms=provider_latency_ms,
            )
            response = QuerySearchResponse(
                recovery=SearchRecoveryRead(
                    reason=failure_reason,
                    detected_query_type=query_type,
                    suggestions=[],
                ),
                diagnostics=diagnostics,
            )
            _record_backend_search_failure(
                session=session,
                user=user,
                query=normalized_term,
                diagnostics=diagnostics,
                structured_args=resolved_call.args,
                app_version=app_version,
                build_number=build_number,
                analytics_environment=analytics_environment,
            )
            session.commit()
            return response

        provider_latency_ms = _elapsed_ms(provider_started_at)
        provider_result_count = _search_result_count(flights)
        provider_outcome = "results" if provider_result_count > 0 else "no_match"
        _log_search_provider(
            query_type=query_type,
            outcome=provider_outcome,
            latency_ms=provider_latency_ms,
            result_count=provider_result_count,
        )

        if provider_result_count == 0:
            flights.recovery = ai_service.recovery_for_empty_result(
                query=normalized_term,
                resolved_call=resolved_call,
            )

        flights.diagnostics = SearchDiagnosticsRead(
            query_type=query_type,
            failure_reason=(
                ai_service.failure_reason_for_recovery(flights.recovery)
                if flights.recovery
                else None
            ),
            normalization_applied=normalization_applied,
            provider_result_count=provider_result_count,
            provider_outcome=provider_outcome,
            provider_latency_ms=provider_latency_ms,
        )

        if provider_result_count == 0:
            _record_backend_search_failure(
                session=session,
                user=user,
                query=normalized_term,
                diagnostics=flights.diagnostics,
                structured_args=resolved_call.args,
                app_version=app_version,
                build_number=build_number,
                analytics_environment=analytics_environment,
            )

        session.commit()

        return flights
    except HTTPException as exc:
        session.rollback()
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            recovery = ai_service.recovery_for_empty_result(
                query=normalized_term,
                resolved_call=resolved_call,
            )
            diagnostics = SearchDiagnosticsRead(
                query_type=ai_service.query_type_for_call(resolved_call),
                failure_reason=ai_service.failure_reason_for_recovery(recovery),
                normalization_applied=normalization_applied,
                provider_result_count=0,
                provider_outcome="not_found",
            )
            response = QuerySearchResponse(
                recovery=recovery,
                diagnostics=diagnostics,
            )
            _record_backend_search_failure(
                session=session,
                user=user,
                query=normalized_term,
                diagnostics=diagnostics,
                structured_args=(resolved_call.args if resolved_call else None),
                app_version=app_version,
                build_number=build_number,
                analytics_environment=analytics_environment,
            )
            session.commit()
            return response
        raise
    except Exception:
        session.rollback()
        logger.exception(
            "Error searching for flight query_type=%s user_id=%s",
            ai_service.query_type_for_call(resolved_call),
            user.id,
        )
        try:
            diagnostics = SearchDiagnosticsRead(
                query_type=ai_service.query_type_for_call(resolved_call),
                failure_reason="internal_error",
                normalization_applied=normalization_applied,
                provider_result_count=0,
                provider_outcome="internal_error",
            )
            _record_backend_search_failure(
                session=session,
                user=user,
                query=normalized_term,
                diagnostics=diagnostics,
                structured_args=(resolved_call.args if resolved_call else None),
                app_version=app_version,
                build_number=build_number,
                analytics_environment=analytics_environment,
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Unable to persist failed-search diagnostic")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.post(
    "/search/term",
    summary="Search for flights without placing private query text in access logs",
    response_model=QuerySearchResponse,
)
async def search_flights_from_text_post(
    payload: SearchQueryRequest,
    accept_language: str | None = Header(default=None),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    return await search_flights_from_text(
        term=payload.term,
        language=payload.language,
        app_version=payload.app_version,
        build_number=payload.build_number,
        analytics_environment=payload.analytics_environment,
        accept_language=accept_language,
        session=session,
        user=user,
    )


@router.post("/search/failures", response_model=dict)
def report_app_search_failure(
    payload: SearchFailureReportRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    try:
        sample = SearchFailureService.record(
            session=session,
            user_id=user.id,
            query=payload.query,
            source=payload.source,
            query_type=payload.query_type,
            failure_reason=payload.failure_reason,
            provider_outcome=payload.provider_outcome,
            normalization_applied=payload.normalization_applied,
            provider_result_count=payload.provider_result_count,
            filtered_result_count=payload.filtered_result_count,
            provider_latency_ms=payload.provider_latency_ms,
            search_journey_id=payload.search_journey_id,
            search_attempt_number=payload.search_attempt_number,
            app_version=payload.app_version,
            build_number=payload.build_number,
            analytics_environment=payload.analytics_environment,
            sample_id=payload.failure_sample_id,
        )
        session.commit()
        return {
            "detail": "success",
            "failure_sample_id": sample.id,
            "retention_days": RETENTION_DAYS,
        }
    except Exception:
        session.rollback()
        logger.exception(
            "Unable to persist app failed-search diagnostic user_id=%s",
            user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.get(
    "/search/failures/report",
    dependencies=[Depends(check_lambda_auth_token)],
)
def get_search_failure_report(
    days: int = Query(default=7, ge=1, le=RETENTION_DAYS),
    limit: int = Query(default=100, ge=1, le=500),
    include_samples: bool = Query(default=False),
    analytics_environment: str | None = Query(default=None, max_length=20),
    session: Session = Depends(get_session),
):
    now_ms = int(time.time() * 1_000)
    SearchFailureService.purge_expired(session, now_ms=now_ms)
    all_samples = SearchFailureService.recent(
        session,
        since_ms=now_ms - days * 24 * 60 * 60 * 1_000,
        limit=5_000,
    )
    session.commit()

    if analytics_environment:
        all_samples = [
            sample
            for sample in all_samples
            if sample.analytics_environment == analytics_environment
        ]

    grouped: dict[tuple[str, str, str, str], dict] = {}
    recurring_queries: dict[str, int] = {}
    for sample in all_samples:
        key = (
            sample.failure_reason,
            sample.query_type,
            sample.source,
            sample.analytics_environment,
        )
        group = grouped.setdefault(
            key,
            {
                "failure_reason": sample.failure_reason,
                "query_type": sample.query_type,
                "source": sample.source,
                "analytics_environment": sample.analytics_environment,
                "count": 0,
                "provider_result_count": 0,
                "filtered_result_count": 0,
            },
        )
        group["count"] += 1
        group["provider_result_count"] += sample.provider_result_count
        group["filtered_result_count"] += sample.filtered_result_count
        recurring_queries[sample.query_digest] = (
            recurring_queries.get(sample.query_digest, 0) + 1
        )

    recent_samples = []
    for sample in all_samples[:limit]:
        item = {
            "id": sample.id,
            "created_at_ms": sample.created_at_ms,
            "expires_at_ms": sample.expires_at_ms,
            "source": sample.source,
            "query_type": sample.query_type,
            "failure_reason": sample.failure_reason,
            "provider_outcome": sample.provider_outcome,
            "normalization_applied": sample.normalization_applied,
            "provider_result_count": sample.provider_result_count,
            "filtered_result_count": sample.filtered_result_count,
            "provider_latency_ms": sample.provider_latency_ms,
            "search_journey_id": sample.search_journey_id,
            "search_attempt_number": sample.search_attempt_number,
            "app_version": sample.app_version,
            "build_number": sample.build_number,
            "analytics_environment": sample.analytics_environment,
            "repeat_count": recurring_queries[sample.query_digest],
            "structured_query": {
                "airline_iata": sample.airline_iata,
                "flight_number": sample.flight_number,
                "departure_airport_iata": sample.departure_airport_iata,
                "arrival_airport_iata": sample.arrival_airport_iata,
                "airport_iata": sample.airport_iata,
                "departure_date": sample.departure_date,
                "direction": sample.direction,
            },
        }
        if include_samples:
            item["redacted_query"] = SearchFailureService.decrypt_query(
                sample.query_ciphertext
            )
        recent_samples.append(item)

    return {
        "retention_days": RETENTION_DAYS,
        "window_days": days,
        "analytics_environment": analytics_environment,
        "sample_count": len(all_samples),
        "groups": sorted(
            grouped.values(),
            key=lambda item: (-item["count"], item["failure_reason"]),
        ),
        "recent_samples": recent_samples,
    }


@router.get(
    "/exact",
    summary="Get the exact single flight instance by its identifying info",
    response_model=FlightRead,
)
async def get_exact_flight(
    departure_date: str = Query(
        ..., description="Flight scheduled date (UTC) e.g. 2025-10-13"
    ),
    flight_number: str = Query(..., description="Flight number e.g. 1061"),
    airline_iata: str = Query(..., description="Airline iata e.g. AF"),
    departure_airport_iata: str = Query(
        ..., description="Departure airport IATA code e.g. EVN"
    ),
    # arrival_airport_iata: str = Query(..., description="Arrival airport IATA code e.g. EVN"),
    scheduled_time_local: str | None = Query(
        None, description="Scheduled UTC departure time, can be null"
    ),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    Get a single precise flight instance from AeroDataBox based on:
    - flight number
    - departure date
    - departure airport
    - scheduled departure time (UTC)
    """
    if not user_has_active_subscription(user=user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not Allowed")

    # NOTE: check case of flight spaning for two days
    new_departure_data = (
        scheduled_time_local.split(" ")[0] if scheduled_time_local else departure_date
    )

    try:
        flights = await FlightService.get_flights(
            session=session,
            departure_date=new_departure_data,
            flight_number=flight_number,
            airline_iata=airline_iata,
        )

        if not flights:
            logger.warning(
                f"Unable to fetch exact flight for departure_date={departure_date}, flight_number={flight_number}, airline_iata={airline_iata}, departure_airport_iata={departure_airport_iata}, scheduled_time_utc={scheduled_time_local} from user id={user}"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Exact flight not found"
            )

        for flight in flights:
            dep = flight.departure
            correct_scheduled_time_local = normalize_offset(scheduled_time_local) if scheduled_time_local else scheduled_time_local
            if (
                dep
                and dep.airport
                and dep.airport.iata == departure_airport_iata
                and dep.scheduled_time_local == correct_scheduled_time_local
            ):
                existing_link = session.exec(select(UserFlightLink).where(
                    UserFlightLink.user_id == user.id,
                    UserFlightLink.flight_id == flight.id
                )).first()

                if not existing_link:
                    FlightPersistence.link_flight_and_user(
                        session=session, flight_id=flight.id, user_id=user.id  # type: ignore
                    )

                user.has_searched = True
                session.commit()

                return flight
        airport_flight = await get_exact_flight_temp(flight_number=flight_number, departure_date=new_departure_data, departure_airport_iata=departure_airport_iata, airline_iata=airline_iata, local_departure_time=scheduled_time_local, session=session)
        if airport_flight:
            return airport_flight

        logger.warning(
            f"Unable to find exact flight for departure_date={departure_date}, flight_number={flight_number}, airline_iata={airline_iata}, departure_airport_iata={departure_airport_iata}, scheduled_time_local={scheduled_time_local} from user id={user} after filtering fetched flights"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Exact flight not found"
        )
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        logger.exception(
            f"Unable to find exact flight for departure_date={departure_date}, flight_number={flight_number}, airline_iata={airline_iata}, departure_airport_iata={departure_airport_iata}, scheduled_time_local={scheduled_time_local} from user id={user}."
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


async def get_all_results(flight_number: str, departure_date: str, departure_airport_iata: str, local_departure_time: str | None):
    result = await FlightService.get_airport_flights(airport_iata=departure_airport_iata, departure_date=departure_date, direction="Departure")

    departures = result.departures
    if not departures:
        return None

    for f in departures:
        if f.number.strip().replace(" ", "") == flight_number.strip().replace(" ", "") and f.departure:
            if local_departure_time is None:
                return f
            elif f.departure.scheduledTime and f.departure.scheduledTime.local == normalize_offset(local_departure_time):
                return f

    return None

async def get_exact_flight_temp(flight_number: str, departure_date: str, departure_airport_iata: str, airline_iata: str, local_departure_time: str | None, session: Session):
    from ..services.flight.persistence import FlightPersistence
    from ..models.aerodatabox import AerodataboxAirport
    from ..models.flight import Departure, Arrival
    import json

    f = await get_all_results(flight_number=flight_number, departure_date=departure_date, departure_airport_iata=departure_airport_iata, local_departure_time=local_departure_time)
    if not f or not f.departure or not f.arrival:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    airline_name = f.airline.name if f.airline else ""
    airline_icao = f.airline.icao if f.airline and f.airline.icao else ""
    airline = FlightPersistence.get_or_create_airline(airline_iata=airline_iata, airline_icao=airline_icao, airline_name=airline_name, session=session)

    dep_airport_aerodatabox = AerodataboxAirport(
        name="unknown",
        iata=departure_airport_iata,
    )
    dep_airport = FlightPersistence.get_or_create_airport_via_iata(aerodatabox_airport=dep_airport_aerodatabox, session=session)
    arr_airport_aerodatabox = AerodataboxAirport(
        name="unknown",
        iata=f.arrival.airport.iata if f.arrival.airport else None,
    )
    arr_airport = FlightPersistence.get_or_create_airport_via_iata(aerodatabox_airport=arr_airport_aerodatabox, session=session)

    departure = Departure(
        terminal=f.departure.terminal,
        gate=f.departure.gate,
        baggage_belt=f.departure.baggageBelt,
        checkin_desk=f.departure.checkInDesk,
        scheduled_time_local=f.departure.scheduledTime.local if f.departure.scheduledTime else None,
        scheduled_time_utc=f.departure.scheduledTime.utc if f.departure.scheduledTime else None,
        revised_time_local=f.departure.revisedTime.local if f.departure.revisedTime else None,
        revised_time_utc=f.departure.revisedTime.utc if f.departure.revisedTime else None,
        predicted_time_local=f.departure.predictedTime.local if f.departure.predictedTime else None,
        predicted_time_utc=f.departure.predictedTime.utc if f.departure.predictedTime else None,
        runway_time_local=f.departure.runwayTime.local if f.departure.runwayTime else None,
        runway_time_utc=f.departure.runwayTime.utc if f.departure.runwayTime else None,
        quality=json.dumps(f.departure.quality),
        airport=dep_airport
    )

    arrival = Arrival(
        terminal=f.arrival.terminal,
        gate=f.arrival.gate,
        baggage_belt=f.arrival.baggageBelt,
        checkin_desk=f.arrival.checkInDesk,
        scheduled_time_local=f.arrival.scheduledTime.local if f.arrival.scheduledTime else None,
        scheduled_time_utc=f.arrival.scheduledTime.utc if f.arrival.scheduledTime else None,
        revised_time_local=f.arrival.revisedTime.local if f.arrival.revisedTime else None,
        revised_time_utc=f.arrival.revisedTime.utc if f.arrival.revisedTime else None,
        predicted_time_local=f.arrival.predictedTime.local if f.arrival.predictedTime else None,
        predicted_time_utc=f.arrival.predictedTime.utc if f.arrival.predictedTime else None,
        runway_time_local=f.arrival.runwayTime.local if f.arrival.runwayTime else None,
        runway_time_utc=f.arrival.runwayTime.utc if f.arrival.runwayTime else None,
        quality=json.dumps(f.arrival.quality),
        airport=arr_airport
    )

    flight = Flight(
        date=departure_date,
        number=flight_number,
        status=f.status,
        airline=airline,
        departure=departure,
        arrival=arrival
    )

    session.add(flight)
    session.commit()
    return flight
