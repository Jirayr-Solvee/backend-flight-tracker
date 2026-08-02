import logging
import time

from fastapi import (APIRouter, BackgroundTasks, Depends, Header, HTTPException,
                     Query, status)
from sqlmodel import Session, select

from ..background_tasks import create_webhook_for_flight
from ..dependency import get_current_user
from ..models import get_session
from ..models.flight import (
    CopilotTelemetryRead,
    Flight,
    FlightRead,
    GlobalFlightResolveCandidatesRequest,
    GlobalFlightPositionRead,
    QuerySearchResponse,
)
from ..models.live_activity import LiveActivityRegistration
from ..models.user import User, UserFlightLink
from ..services.flight.delay_risk import DelayRiskResponse, DelayRiskService
from ..services.flight import FlightPersistence, FlightService
from ..services.gemini.service import GeminiService
from ..utils import user_has_active_subscription, normalize_offset

router = APIRouter()

logger = logging.getLogger(__name__)


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
    term: str = Query(..., min_length=3),
    language: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    try:
        ai_service = GeminiService()
        result = await ai_service.get_function_call(
            query=term,
            language=language or accept_language,
        )

        if not result:
            logger.warning(
                f"Gemini unable to retrive a function call from user query={term}"
            )
            return QuerySearchResponse()

        flights = await result.handler(**result.args, session=session)

        session.commit()

        return flights
    except HTTPException as exc:
        session.rollback()
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return QuerySearchResponse()
        raise
    except Exception:
        session.rollback()
        logger.exception(f"Error searching for flight using term={term}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


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
