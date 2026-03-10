import logging

from fastapi import (APIRouter, BackgroundTasks, Depends, HTTPException, Query,
                     status)
from sqlmodel import Session, select

from ..background_tasks import create_webhook_for_flight
from ..dependency import get_current_user
from ..models import get_session
from ..models.flight import Flight, FlightRead, QuerySearchResponse
from ..models.user import User, UserFlightLink
from ..services.flight import FlightPersistence, FlightService
from ..services.gemini.service import GeminiService
from ..utils import user_has_active_subscription

router = APIRouter()

logger = logging.getLogger(__name__)


# TODO: delete
@router.get("/", summary="List all flights from DB", response_model=list[FlightRead])
def list_flights(session: Session = Depends(get_session)):
    try:
        flights = session.exec(select(Flight)).all()
        return flights
    except Exception:
        session.rollback()
        logger.exception(f"Unable to fetch all flights from db")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.get("/all", summary="List all flights from DB")
def list_flights(session: Session = Depends(get_session)):
    try:
        flights = session.exec(select(Flight)).all()
        return flights
    except Exception:
        session.rollback()
        logger.exception(f"Unable to fetch all flights from db")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.get("/11", summary="List all flights from DB")
def list_flights2(session: Session = Depends(get_session)):
    try:
        status_1 = ""
        status_2 = ""

        f = session.get(Flight, 1)
        status_1 = f.status

        f.status = "Unkown"

        flights = session.exec(select(Flight)).all()

        return {"old_status": status_1, "hopfully_updated": [f.status for f in flights]}
    except Exception:
        session.rollback()
        logger.exception(f"Unable to fetch all flights from db")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


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

        background_tasks.add_task(create_webhook_for_flight, flight.number)

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


@router.get(
    "/search/term",
    summary="Search for flights using nomral text",
    response_model=QuerySearchResponse,
)
async def search_flights_from_text(
    term: str = Query(..., min_length=3),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    if not user_has_active_subscription(user=user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not Allowed")
    try:
        ai_service = GeminiService()
        result = await ai_service.get_function_call(query=term)

        if not result:
            logger.warning(
                f"Gemini unable to retrive a function call from user query={term}"
            )
            return QuerySearchResponse()

        flights = await result.handler(**result.args, session=session)

        session.commit()

        return flights
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
    background_tasks: BackgroundTasks,
    departure_date: str = Query(
        ..., description="Flight scheduled date (UTC) e.g. 2025-10-13"
    ),
    flight_number: str = Query(..., description="Flight number e.g. 1061"),
    airline_iata: str = Query(..., description="Airline iata e.g. AF"),
    departure_airport_iata: str = Query(
        ..., description="Departure airport IATA code e.g. EVN"
    ),
    # arrival_airport_iata: str = Query(..., description="Arrival airport IATA code e.g. EVN"),
    scheduled_time_utc: str | None = Query(
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
        scheduled_time_utc.split(" ")[0] if scheduled_time_utc else departure_date
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
                f"Unable to fetch exact flight for departure_date={departure_date}, flight_number={flight_number}, airline_iata={airline_iata}, departure_airport_iata={departure_airport_iata}, scheduled_time_utc={scheduled_time_utc} from user id={user}"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Exact flight not found"
            )

        for flight in flights:
            dep = flight.departure

            if (
                dep
                and dep.airport
                and dep.airport.iata == departure_airport_iata
                and dep.scheduled_time_utc == scheduled_time_utc
            ):
                FlightPersistence.link_flight_and_user(
                    session=session, flight_id=flight.id, user_id=user.id  # type: ignore
                )
                background_tasks.add_task(create_webhook_for_flight, flight.number)

                user.has_searched = True
                session.commit()

                return flight
        logger.warning(
            f"Unable to find exact flight for departure_date={departure_date}, flight_number={flight_number}, airline_iata={airline_iata}, departure_airport_iata={departure_airport_iata}, scheduled_time_utc={scheduled_time_utc} from user id={user} after filtering fetched flights"
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
            f"Unable to find exact flight for departure_date={departure_date}, flight_number={flight_number}, airline_iata={airline_iata}, departure_airport_iata={departure_airport_iata}, scheduled_time_utc={scheduled_time_utc} from user id={user}."
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
