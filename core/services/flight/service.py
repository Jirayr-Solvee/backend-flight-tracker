from datetime import datetime
from enum import Enum
from typing import Sequence

from sqlmodel import Session

from ...models.aerodatabox import AirportFidsContract, FlightStatusEnum
from ...models.flight import (
    AirportFlightOriginAndDestinationInfoRead,
    AirportFlightRead,
    Flight,
    FlightRead,
    QuerySearchResponse,
)
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
    MAX_EARLY_DEPARTURE_MINUTES = 180
    MAX_EARLY_ARRIVAL_MINUTES = 360
    MAX_LATE_UPDATE_MINUTES = 24 * 60

    STATUS_SCORE = {
        FlightStatusEnum.UNKNOWN.value: 0,
        FlightStatusEnum.EXPECTED.value: 10,
        FlightStatusEnum.CHECKIN.value: 20,
        FlightStatusEnum.BOARDING.value: 30,
        FlightStatusEnum.GATECLOSED.value: 40,
        FlightStatusEnum.DELAYED.value: 45,
        FlightStatusEnum.CANCELEDUNCERTAIN.value: 50,
        FlightStatusEnum.CANCELED.value: 55,
        FlightStatusEnum.DIVERTED.value: 60,
        FlightStatusEnum.DEPARTED.value: 70,
        FlightStatusEnum.ENROUTE.value: 80,
        FlightStatusEnum.APPROACHING.value: 90,
        FlightStatusEnum.ARRIVED.value: 100,
    }

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

        return QuerySearchResponse(
            airport_flights_result=FlightQueryHandler._dedupe_airport_flights(
                filtered_flights
            )
        )

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

        return QuerySearchResponse(
            airport_flights_result=FlightQueryHandler._dedupe_airport_flights(
                filtered_flights
            )
        )

    @staticmethod
    def _dedupe_airport_flights(
        flights: list[AirportFlightRead],
    ) -> list[AirportFlightRead]:
        best_by_key: dict[tuple[str, str, str, str, str], AirportFlightRead] = {}

        for flight in flights:
            FlightQueryHandler._sanitize_airport_flight(flight)

            key = FlightQueryHandler._airport_flight_key(flight)
            current = best_by_key.get(key)
            if (
                current is None
                or FlightQueryHandler._airport_flight_score(flight)
                > FlightQueryHandler._airport_flight_score(current)
            ):
                best_by_key[key] = flight

        return list(best_by_key.values())

    @staticmethod
    def _airport_flight_key(
        flight: AirportFlightRead,
    ) -> tuple[str, str, str, str, str]:
        return (
            flight.number.strip().replace(" ", "").upper(),
            flight.departure.scheduled_time_utc if flight.departure else "",
            flight.arrival.scheduled_time_utc if flight.arrival else "",
            (flight.departure.airport.iata if flight.departure else "") or "",
            (flight.arrival.airport.iata if flight.arrival else "") or "",
        )

    @staticmethod
    def _airport_flight_score(flight: AirportFlightRead) -> int:
        status_value = (
            flight.status.value if isinstance(flight.status, FlightStatusEnum)
            else str(flight.status)
        )
        score = FlightQueryHandler.STATUS_SCORE.get(status_value, 0)

        for segment in (flight.departure, flight.arrival):
            if not segment:
                continue

            if segment.runway_time_utc:
                score += 30
            if segment.predicted_time_utc:
                score += 20
            if segment.revised_time_utc:
                score += 10

        return score

    @staticmethod
    def _sanitize_airport_flight(flight: AirportFlightRead) -> None:
        if flight.departure:
            FlightQueryHandler._sanitize_airport_time_segment(
                flight.departure,
                max_early_minutes=FlightQueryHandler.MAX_EARLY_DEPARTURE_MINUTES,
            )

        if flight.arrival:
            FlightQueryHandler._sanitize_airport_time_segment(
                flight.arrival,
                max_early_minutes=FlightQueryHandler.MAX_EARLY_ARRIVAL_MINUTES,
            )

    @staticmethod
    def _sanitize_airport_time_segment(
        segment: AirportFlightOriginAndDestinationInfoRead,
        max_early_minutes: int,
    ) -> None:
        for field in ("runway", "predicted", "revised"):
            update_utc = getattr(segment, f"{field}_time_utc")
            if not FlightQueryHandler._is_reasonable_update(
                scheduled_utc=segment.scheduled_time_utc,
                update_utc=update_utc,
                max_early_minutes=max_early_minutes,
            ):
                setattr(segment, f"{field}_time_utc", None)
                setattr(segment, f"{field}_time_local", None)

    @staticmethod
    def _is_reasonable_update(
        scheduled_utc: str | None,
        update_utc: str | None,
        max_early_minutes: int,
    ) -> bool:
        if not scheduled_utc or not update_utc:
            return True

        scheduled = FlightQueryHandler._parse_airport_timestamp(scheduled_utc)
        update = FlightQueryHandler._parse_airport_timestamp(update_utc)
        if not scheduled or not update:
            return True

        delta_minutes = int((update - scheduled).total_seconds() // 60)
        if delta_minutes < -max_early_minutes:
            return False

        return delta_minutes <= FlightQueryHandler.MAX_LATE_UPDATE_MINUTES

    @staticmethod
    def _parse_airport_timestamp(value: str) -> datetime | None:
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%MZ")
        except ValueError:
            return None
