import json
import logging
from typing import Sequence

from sqlmodel import Session, select, case

from ...models.aerodatabox import (
    AerodataboxAirport, AerodataboxFlight,
    AerodataboxOriginAndDestinationInformationWebhook,
    FlightNotificationContractItem)
from ...models.flight import Airline, Airport, Arrival, Departure, Flight
from ...models.user import UserFlightLink
from ...utils import get_time
from .mapper import FlightMapper

logger = logging.getLogger(__name__)


class FlightPersistence:

    @staticmethod
    def create_user_flight_link(
        session: Session, flight_id: int, user_id: str
    ) -> UserFlightLink:
        link = UserFlightLink(flight_id=flight_id, user_id=user_id)
        session.add(link)
        return link

    @staticmethod
    def get_user_flight_link(
        session: Session, flight_id: int, user_id: str
    ) -> UserFlightLink | None:
        return session.exec(
            select(UserFlightLink).where(
                UserFlightLink.flight_id == flight_id, UserFlightLink.user_id == user_id
            )
        ).first()

    @staticmethod
    def delete_user_flight_link(session: Session, flight_id: int, user_id: str) -> bool:
        link = session.exec(
            select(UserFlightLink).where(
                UserFlightLink.flight_id == flight_id, UserFlightLink.user_id == user_id
            )
        ).first()

        if not link:
            logger.warning(
                f"unable to find UserFlightLink for flight_id={flight_id}, user_id={user_id}"
            )
            return False

        session.delete(link)
        return True

    @staticmethod
    def link_flight_and_user(session: Session, flight_id: int, user_id: str):
        link = UserFlightLink(user_id=user_id, flight_id=flight_id)
        session.add(link)

    @staticmethod
    def get_flights(
        session: Session, full_number: str, departure_date: str
    ) -> Sequence[Flight]:
        stmt = select(Flight).where(
            Flight.date == departure_date, Flight.number == full_number
        )
        return session.exec(stmt).all()

    @staticmethod
    def create_flights_from_aerodatabox_model(
        flights: list[AerodataboxFlight],
        airline_iata: str,
        departure_date: str,
        session: Session,
    ) -> list[Flight]:
        """Create list of flights models form aerodatabox models"""
        try:

            airline_name = ""
            airline_icao = ""

            for f in flights:
                if not f.airline:
                    continue
                
                # there is a tempo bug on aerodatabox
                if f.airline.iata:
                    airline_iata = f.airline.iata

                if airline_name == "" and f.airline.name:
                    airline_name = f.airline.name

                if airline_icao == "" and f.airline.icao:
                    airline_icao = f.airline.icao

                if airline_name and airline_icao:
                    break

            # 1. create airline if not found
            airline = FlightPersistence.get_or_create_airline(
                airline_iata=airline_iata,
                airline_name=airline_name,
                airline_icao=airline_icao,
                session=session,
            )

            # 2. create the whole flight model
            database_flights = [
                FlightPersistence.create_single_flight_from_aerodatabox_model(
                    flight=f,
                    airline=airline,
                    departure_date=departure_date,
                    session=session,
                )
                for f in flights
            ]

            session.flush()

            return database_flights
        except Exception:
            session.rollback()
            return []

    @staticmethod
    def get_or_create_airline(
        airline_iata: str, airline_name: str, airline_icao: str, session: Session
    ) -> Airline:
        """Get Airline from database or create one"""
        airline = session.exec(
            select(Airline).where(Airline.iata == airline_iata.strip().upper())
        ).first()

        if airline:
            return airline

        airline = Airline(name=airline_name, iata=airline_iata, icao=airline_icao)
        session.add(airline)
        return airline

    @staticmethod
    def get_or_create_airport(
        aerodatabox_airport: AerodataboxAirport, session: Session
    ) -> Airport:
        """Get Airport from database or create one"""
        airport = session.exec(
            select(Airport).where(Airport.name == aerodatabox_airport.name)
        ).first()

        if airport:
            return airport

        airport = FlightMapper.aero_airport_to_flight_airport(
            aerodatabox_airport=aerodatabox_airport
        )
        session.add(airport)
        return airport

    @staticmethod
    def create_single_flight_from_aerodatabox_model(
        flight: AerodataboxFlight,
        airline: Airline,
        departure_date: str,
        session: Session,
    ) -> Flight:
        dep_airport = FlightPersistence.get_or_create_airport(
            aerodatabox_airport=flight.departure.airport, session=session
        )
        arr_airport = FlightPersistence.get_or_create_airport(
            aerodatabox_airport=flight.arrival.airport, session=session
        )

        departure = FlightMapper.aero_departure_to_flight_departure(
            aero_departure=flight.departure, airport=dep_airport
        )
        session.add(departure)

        arrival = FlightMapper.aero_arrival_to_flight_arrival(
            aero_arrival=flight.arrival, airport=arr_airport
        )
        session.add(arrival)

        database_flight = Flight(
            number=flight.number.strip().replace(" ", ""),
            status=flight.status,
            date=departure_date,
            distance_km=(
                flight.greatCircleDistance.km if flight.greatCircleDistance else None
            ),
            distance_mile=(
                flight.greatCircleDistance.mile if flight.greatCircleDistance else None
            ),
            airline=airline,
            departure=departure,
            arrival=arrival,
            aircraft_model=flight.aircraft.model if flight.aircraft else None,
            aircraft_modeS=flight.aircraft.modeS if flight.aircraft else None,
            aircraft_reg=flight.aircraft.reg if flight.aircraft else None,
        )

        session.add(database_flight)
        return database_flight

    @staticmethod
    def update_flight_from_webhook_data(
        flight: Flight, webhook_flight: FlightNotificationContractItem
    ):
        # extract single level data from here

        basic_data_map = {
            "status": webhook_flight.status,
            "aircraft_reg": (
                webhook_flight.aircraft.reg if webhook_flight.aircraft else None
            ),
            "aircraft_modeS": (
                webhook_flight.aircraft.modeS if webhook_flight.aircraft else None
            ),
            "aircraft_model": (
                webhook_flight.aircraft.model if webhook_flight.aircraft else None
            ),
        }
        webhook_flight.departure
        for key, val in basic_data_map.items():
            setattr(flight, key, val)

        # TODO: departure/arrival must be not nullable in db and remove if check from everywhere + guard when creating a flight
        if flight.departure and flight.arrival:
            FlightPersistence.update_flight_deparr_from_webhook_data(
                db_obj=flight.departure, webhook_data=webhook_flight.departure
            )
            FlightPersistence.update_flight_deparr_from_webhook_data(
                db_obj=flight.arrival, webhook_data=webhook_flight.arrival
            )

    @staticmethod
    def update_flight_deparr_from_webhook_data(
        db_obj: Departure | Arrival,
        webhook_data: AerodataboxOriginAndDestinationInformationWebhook,
    ):
        fields = {
            "terminal": webhook_data.terminal,
            "gate": webhook_data.gate,
            "baggage_belt": webhook_data.baggageBelt,
            "checkin_desk": webhook_data.checkInDesk,
            "scheduled_time_local": get_time(webhook_data.scheduledTime, "local"),
            "scheduled_time_utc": get_time(webhook_data.scheduledTime, "utc"),
            "revised_time_local": get_time(webhook_data.revisedTime, "local"),
            "revised_time_utc": get_time(webhook_data.revisedTime, "utc"),
            "predicted_time_local": get_time(webhook_data.predictedTime, "local"),
            "predicted_time_utc": get_time(webhook_data.predictedTime, "utc"),
            "runway_time_local": get_time(webhook_data.runwayTime, "local"),
            "runway_time_utc": get_time(webhook_data.runwayTime, "utc"),
            "quality": json.dumps(webhook_data.quality),
        }

        for key, val in fields.items():
            if val is not None:
                setattr(db_obj, key, val)
    
    @staticmethod
    def get_random_flight(session: Session) -> Flight | None:
        STATUSES_PRIORITY: list[str] = [
            "Expected",
            "CheckIn",
            "Boarding",
            "GateClosed",
            "Departed",
            "EnRoute",
            "Approaching",
        ]

        status_priority = case(
            {status: i for i, status in enumerate(STATUSES_PRIORITY)},
            value=Flight.status,
            else_=100,
        )

        live_score = case(
            (
                (Departure.quality.like("%live%")) &
                (Arrival.quality.like("%live%")),
                0,
            ),
            else_=1,
        )

        statement = (
            select(Flight)
            .where(Flight.status.in_(STATUSES_PRIORITY)) # type: ignore
            .join(Departure, isouter=True)
            .join(Arrival, isouter=True)
            .order_by(live_score, status_priority)
        )

        return session.exec(statement).first()
