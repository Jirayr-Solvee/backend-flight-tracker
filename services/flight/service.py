from enum import Enum
from typing import Sequence

from sqlmodel import Session

from ...models.aerodatabox import AirportFidsContract
from ...models.flight import Flight, FlightRead, QuerySearchResponse
from .api_client import AerodataboxClient
from .mapper import AirportFlightMapper
from .persistence import FlightPersistence


class FlightService:
    @staticmethod
    async def get_flights(
        session: Session,
        departure_date: str,
        flight_number: str,
        airline_iata: str,
    ) -> Sequence[Flight]:
        """Get flights from DATABASE, else from Aerodatabox then save in DATABASE before returning"""
        full_number = f"{airline_iata.strip().upper()}{flight_number}"

        db_flights = FlightPersistence.get_flights(session, full_number, departure_date)
        if db_flights:
            return db_flights

        api_client = AerodataboxClient()

        flights = await api_client.get_flight(
            full_number=full_number, departure_date=departure_date
        )
        if not flights:
            return []

        new_flights = FlightPersistence.create_flights_from_aerodatabox_model(
            flights=flights,
            airline_iata=airline_iata,
            departure_date=departure_date,
            session=session,
        )
        return new_flights

    @staticmethod
    async def get_airport_flights(
        airport_iata: str, departure_date: str, direction: str = "Both"
    ) -> AirportFidsContract:
        """
        Get flights for an airport for the window of 24H from Aerodatabox API.
        """
        api_client = AerodataboxClient()

        return await api_client.get_airport_flights(
            airport_iata=airport_iata,
            departure_date=departure_date,
            direction=direction,
        )


class AirportSearchDirection(str, Enum):
    DEPARTURE = "Departure"
    ARRIVAL = "Arrival"


class FlightQueryHandler:
    @staticmethod
    async def extract_random_flight(
        random: bool,
        session: Session
    ):
        flight = FlightPersistence.get_random_flight(session=session)
        return QuerySearchResponse(
            flights_result=[FlightRead.model_validate(flight, from_attributes=True)] if flight else []
        )

    @staticmethod
    async def extract_flight_info(
        departure_date: str,
        flight_number: str,
        airline_iata: str,
        session: Session,
    ):
        flights = await FlightService.get_flights(
            session=session,
            departure_date=departure_date,
            flight_number=flight_number,
            airline_iata=airline_iata,
        )
        return QuerySearchResponse(
            flights_result=[
                FlightRead.model_validate(flight, from_attributes=True)
                for flight in flights
            ]
        )

    @staticmethod
    async def extract_flight_from_email(
        departure_date: str,
        flight_number: str,
        airline_iata: str,
        session: Session,
    ):
        return await FlightService.get_flights(
            session=session,
            departure_date=departure_date,
            flight_number=flight_number,
            airline_iata=airline_iata,
        )

    @staticmethod
    async def extract_flight_info_via_airport(
        departure_date: str,
        departure_airport_iata: str,
        arrival_airport_iata: str,
        **kwargs,
    ):
        result = await FlightService.get_airport_flights(
            departure_date=departure_date,
            airport_iata=departure_airport_iata,
            direction=AirportSearchDirection.DEPARTURE.value,
        )

        # get only departures
        flights = result.departures

        if not flights:
            return QuerySearchResponse()

        # filter based on arrival iata
        filtered_flights = []
        for flight in flights:
            flight_arrival_iata = (
                flight.arrival.airport.iata
                if flight.arrival and flight.arrival.airport
                else None
            )

            if (
                flight.departure
                and flight.departure.airport
                and flight.arrival
                and flight_arrival_iata
                and flight_arrival_iata.upper() == arrival_airport_iata.upper()
            ):
                flight.departure.airport.iata = departure_airport_iata
                f_read = AirportFlightMapper.airport_flight_to_airport_flight_read(
                    flight=flight,
                    departure_date=departure_date,
                    departure=flight.departure,
                    arrival=flight.arrival,
                    departure_iata=departure_airport_iata,
                    arrival_iata=arrival_airport_iata,
                )
                filtered_flights.append(f_read)

        return QuerySearchResponse(airport_flights_result=filtered_flights)

    @staticmethod
    async def extract_flight_info_via_airport_single_derection(
        departure_date: str,
        airport_iata: str,
        direction: AirportSearchDirection,
        **kwargs,
    ):
        result = await FlightService.get_airport_flights(
            departure_date=departure_date,
            airport_iata=airport_iata,
            direction=direction,
        )

        if direction == AirportSearchDirection.DEPARTURE.value:
            flights = result.departures
        else:
            flights = result.arrivals

        if not flights:
            return QuerySearchResponse()

        # append the actual iata for each of them
        from .utils import append_iatas

        flights = append_iatas(direction=direction, iata=airport_iata, flights=flights)

        filtered_flights = []
        for flight in flights:
            if (
                flight.departure
                and flight.departure.airport
                and flight.departure.airport.iata
                and flight.arrival
                and flight.arrival.airport
                and flight.arrival.airport.iata
            ):
                f_read = AirportFlightMapper.airport_flight_to_airport_flight_read(
                    flight=flight,
                    departure_date=departure_date,
                    departure=flight.departure,
                    arrival=flight.arrival,
                    departure_iata=flight.departure.airport.iata,
                    arrival_iata=flight.arrival.airport.iata,
                )

                filtered_flights.append(f_read)

        return QuerySearchResponse(airport_flights_result=filtered_flights)
