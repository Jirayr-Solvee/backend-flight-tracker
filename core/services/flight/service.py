import json
import logging
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Sequence

from sqlmodel import Session

from ...config import settings
from ...models.aerodatabox import (
    AerodataboxFlight,
    AerodataboxFlightLocation,
    AirportFidsContract,
    FlightStatusEnum,
)
from ...models.flight import (
    AirportFlightOriginAndDestinationInfoRead,
    AirportFlightRead,
    CopilotTelemetryRead,
    Flight,
    FlightRead,
    GlobalFlightPositionRead,
    QuerySearchResponse,
)
from .api_client import ADSBExchangeClient, AerodataboxClient
from .mapper import AirportFlightMapper
from .persistence import FlightPersistence

logger = logging.getLogger(__name__)


class FlightService:
    _global_positions_cache: tuple[datetime, list[GlobalFlightPositionRead]] | None = None
    _iata_by_icao_cache: dict[str, str] | None = None
    _iata_by_icao_overrides = {
        "DLH": "LH",
        "GLO": "G3",
        "SIA": "SQ",
    }
    _route_callsign_pattern = re.compile(r"^([A-Z]{2,3})([0-9]{1,4})$")
    _major_airlines_by_continent: dict[str, set[str]] = {
        "north_america": {
            "AA",
            "AAL",
            "AC",
            "ACA",
            "DL",
            "DAL",
            "UA",
            "UAL",
            "WN",
            "SWA",
        },
        "south_america": {
            "AD",
            "AZU",
            "AV",
            "AVA",
            "CM",
            "CMP",
            "G3",
            "GLO",
            "LA",
            "LAN",
            "JJ",
            "TAM",
        },
        "europe": {
            "AF",
            "AFR",
            "BA",
            "BAW",
            "KL",
            "KLM",
            "LH",
            "DLH",
            "TK",
            "THY",
        },
        "africa": {
            "EK",
            "UAE",
            "KQ",
            "KQA",
            "MS",
            "MSR",
            "SA",
            "SAA",
            "AT",
            "RAM",
        },
        "asia": {
            "CX",
            "CPA",
            "EK",
            "UAE",
            "QR",
            "QTR",
            "SQ",
            "SIA",
            "TK",
            "THY",
        },
        "oceania": {
            "FJ",
            "FJI",
            "JQ",
            "JST",
            "NZ",
            "ANZ",
            "QF",
            "QFA",
            "VA",
            "VOZ",
        },
    }

    @staticmethod
    async def get_global_flight_positions(limit: int | None = None) -> list[GlobalFlightPositionRead]:
        now = datetime.now(timezone.utc)
        cached = FlightService._global_positions_cache
        ttl_seconds = max(1, settings.ADSBEXCHANGE_CACHE_TTL_SECONDS)
        if cached and now - cached[0] < timedelta(seconds=ttl_seconds):
            positions = cached[1]
        else:
            try:
                positions = await ADSBExchangeClient().get_global_positions()
                FlightService._global_positions_cache = (now, positions)
            except Exception:
                logger.exception("Unable to fetch global flight positions")
                positions = cached[1] if cached else []

        response_limit = settings.GLOBAL_FLIGHT_POSITIONS_RESPONSE_LIMIT
        if limit is not None:
            response_limit = min(limit, response_limit)

        positions = FlightService._filter_global_positions_on_route(positions, now=now)

        return FlightService._sample_global_positions_by_continent(
            positions=positions,
            per_continent_limit=settings.GLOBAL_FLIGHT_POSITIONS_PER_CONTINENT_LIMIT,
            overall_limit=response_limit,
        )

    @staticmethod
    def _filter_global_positions_on_route(
        positions: Sequence[GlobalFlightPositionRead],
        now: datetime,
    ) -> list[GlobalFlightPositionRead]:
        now_timestamp = int(now.timestamp())
        return [
            position
            for position in positions
            if FlightService._global_position_is_on_route(
                position=position,
                now_timestamp=now_timestamp,
            )
        ]

    @staticmethod
    def _global_position_is_on_route(
        position: GlobalFlightPositionRead,
        now_timestamp: int,
    ) -> bool:
        if position.on_ground:
            return False

        if not FlightService._looks_like_route_callsign(position):
            return False

        continent = FlightService._continent_key(lat=position.lat, lon=position.lon)
        if (
            settings.GLOBAL_FLIGHT_MAJOR_AIRLINES_ONLY
            and not FlightService._is_preferred_global_airline(
                position=position,
                continent=continent,
            )
        ):
            return False

        FlightService._normalize_position_callsign_for_tracking(position)

        latest_contact = position.time_position or position.last_contact
        if latest_contact is None:
            return False

        if now_timestamp - latest_contact > 300:
            return False

        altitude_feet = position.altitude_feet or 0
        ground_speed_kt = position.ground_speed_kt or (
            position.velocity_mps * 1.943844 if position.velocity_mps is not None else 0
        )

        return altitude_feet >= 5_000 and ground_speed_kt >= 180

    @staticmethod
    def _looks_like_route_callsign(position: GlobalFlightPositionRead) -> bool:
        code = (position.callsign or position.display_code or "").strip().upper()
        if not code or code == position.icao24.upper():
            return False

        if len(code) < 4 or len(code) > 8:
            return False

        if code.startswith("N") and len(code) > 1 and code[1].isdigit():
            return False

        return FlightService._route_callsign_pattern.match(code) is not None

    @staticmethod
    def _is_preferred_global_airline(
        position: GlobalFlightPositionRead,
        continent: str,
    ) -> bool:
        prefix = FlightService._callsign_airline_prefix(position)
        if not prefix:
            return False

        preferred_codes = FlightService._preferred_airline_codes_for_continent(continent)
        if not preferred_codes:
            return False

        normalized_prefix = FlightService._iata_for_airline_prefix(prefix) or prefix
        return prefix in preferred_codes or normalized_prefix in preferred_codes

    @staticmethod
    def _preferred_airline_codes_for_continent(continent: str) -> set[str]:
        preferred_codes = FlightService._major_airlines_by_continent.get(continent)
        if preferred_codes:
            return preferred_codes

        return {
            code
            for continent_codes in FlightService._major_airlines_by_continent.values()
            for code in continent_codes
        }

    @staticmethod
    def _callsign_airline_prefix(position: GlobalFlightPositionRead) -> str | None:
        code = (position.callsign or position.display_code or "").strip().upper()
        match = FlightService._route_callsign_pattern.match(code)
        if not match:
            return None

        return match.group(1)

    @staticmethod
    def _normalize_position_callsign_for_tracking(position: GlobalFlightPositionRead) -> None:
        if not settings.GLOBAL_FLIGHT_NORMALIZE_CALLSIGN_TO_IATA:
            return

        code = (position.callsign or position.display_code or "").strip().upper()
        normalized = FlightService._normalized_callsign_for_tracking(code)
        if not normalized:
            return

        position.callsign = normalized
        position.display_code = normalized

    @staticmethod
    def _normalized_callsign_for_tracking(code: str) -> str | None:
        match = FlightService._route_callsign_pattern.match(code)
        if not match:
            return None

        prefix, number = match.groups()
        iata = FlightService._iata_for_airline_prefix(prefix)
        if not iata:
            return code

        return f"{iata}{number}"

    @staticmethod
    def _iata_for_airline_prefix(prefix: str) -> str | None:
        prefix = prefix.strip().upper()
        if len(prefix) != 3:
            return None

        return FlightService._iata_by_icao().get(prefix)

    @staticmethod
    def _iata_by_icao() -> dict[str, str]:
        if FlightService._iata_by_icao_cache is not None:
            return FlightService._iata_by_icao_cache

        try:
            airline_map_path = Path(settings.AIRLINE_MAP_JSON)
            if not airline_map_path.is_absolute():
                airline_map_path = Path.cwd() / airline_map_path

            with airline_map_path.open("r") as file:
                iata_to_icao = json.load(file)

            FlightService._iata_by_icao_cache = {
                str(icao).strip().upper(): str(iata).strip().upper()
                for iata, icao in iata_to_icao.items()
                if str(iata).strip() and str(icao).strip()
            }
            FlightService._iata_by_icao_cache.update(FlightService._iata_by_icao_overrides)
        except Exception:
            logger.exception("Unable to load airline IATA/ICAO map")
            FlightService._iata_by_icao_cache = FlightService._iata_by_icao_overrides.copy()

        return FlightService._iata_by_icao_cache

    @staticmethod
    def _sample_global_positions_by_continent(
        positions: list[GlobalFlightPositionRead],
        per_continent_limit: int,
        overall_limit: int,
    ) -> list[GlobalFlightPositionRead]:
        if per_continent_limit <= 0 or overall_limit <= 0:
            return []

        grouped: dict[str, list[GlobalFlightPositionRead]] = {}
        for position in positions:
            continent = FlightService._continent_key(
                lat=position.lat,
                lon=position.lon,
            )
            grouped.setdefault(continent, []).append(position)

        continent_order = [
            "north_america",
            "south_america",
            "europe",
            "africa",
            "asia",
            "oceania",
            "antarctica",
            "other",
        ]

        sampled: list[GlobalFlightPositionRead] = []
        for continent in continent_order:
            continent_positions = grouped.get(continent)
            if not continent_positions:
                continue

            sampled.extend(
                FlightService._sample_global_positions(
                    positions=continent_positions,
                    limit=per_continent_limit,
                )
            )

        if len(sampled) <= overall_limit:
            return sampled

        return FlightService._stride_sample(sampled, overall_limit)

    @staticmethod
    def _sample_global_positions(
        positions: list[GlobalFlightPositionRead],
        limit: int,
    ) -> list[GlobalFlightPositionRead]:
        if limit <= 0:
            return []
        if len(positions) <= limit:
            return positions

        buckets: dict[str, GlobalFlightPositionRead] = {}
        remaining: list[GlobalFlightPositionRead] = []

        for position in positions:
            lat_band = int((position.lat + 90) / 8)
            lon_band = int((position.lon + 180) / 8)
            key = f"{lat_band}:{lon_band}"

            if key not in buckets:
                buckets[key] = position
            else:
                remaining.append(position)

        sampled = [buckets[key] for key in sorted(buckets.keys())]
        if len(sampled) > limit:
            return FlightService._stride_sample(sampled, limit)

        sampled.extend(
            FlightService._stride_sample(
                remaining,
                limit - len(sampled),
            )
        )
        return sampled

    @staticmethod
    def _continent_key(lat: float, lon: float) -> str:
        if lat < -60:
            return "antarctica"

        if -90 <= lon <= -30 and -60 <= lat <= 15:
            return "south_america"

        if -170 <= lon <= -20 and lat >= 5:
            return "north_america"

        if -25 <= lon <= 45 and 35 <= lat <= 72:
            return "europe"

        if -20 <= lon <= 55 and -35 <= lat <= 38:
            return "africa"

        if (110 <= lon <= 180 or -180 <= lon <= -120) and -50 <= lat <= 10:
            return "oceania"

        if (25 <= lon <= 180 or -180 <= lon <= -170) and -10 <= lat <= 80:
            return "asia"

        return "other"

    @staticmethod
    def _stride_sample(
        positions: list[GlobalFlightPositionRead],
        limit: int,
    ) -> list[GlobalFlightPositionRead]:
        if limit <= 0:
            return []
        if len(positions) <= limit:
            return positions

        stride = len(positions) / limit
        return [positions[int(index * stride)] for index in range(limit)]

    @staticmethod
    async def resolve_global_live_flight(
        session: Session,
        callsign: str,
        icao24: str | None = None,
    ) -> Flight | None:
        full_number = callsign.strip().replace(" ", "").upper()
        if len(full_number) < 3:
            return None

        today = datetime.now(timezone.utc).date()
        dates = [
            today.isoformat(),
            (today - timedelta(days=1)).isoformat(),
            (today + timedelta(days=1)).isoformat(),
        ]

        api_client = AerodataboxClient()
        for departure_date in dates:
            live_flights = await api_client.get_flight(
                full_number=full_number,
                departure_date=departure_date,
                with_location=True,
            )
            if not live_flights:
                continue

            live_flight = FlightService._select_matching_global_live_flight(
                live_flights=live_flights,
                callsign=full_number,
                icao24=icao24,
            )
            normalized_number = live_flight.number.strip().replace(" ", "").upper()

            db_flights = FlightPersistence.get_flights(
                session=session,
                full_number=normalized_number,
                departure_date=departure_date,
            )
            if db_flights:
                return FlightService._select_matching_db_flight(
                    db_flights=db_flights,
                    live_flight=live_flight,
                    icao24=icao24,
                )

            created = FlightPersistence.create_flights_from_aerodatabox_model(
                flights=live_flights,
                airline_iata=live_flight.airline.iata if live_flight.airline and live_flight.airline.iata else "",
                departure_date=departure_date,
                session=session,
            )
            if created:
                return FlightService._select_matching_db_flight(
                    db_flights=created,
                    live_flight=live_flight,
                    icao24=icao24,
                )

        return None

    @staticmethod
    def _select_matching_global_live_flight(
        live_flights: list[AerodataboxFlight],
        callsign: str,
        icao24: str | None,
    ) -> AerodataboxFlight:
        def normalize(value: str | None) -> str:
            return (value or "").strip().replace(" ", "").upper()

        normalized_icao24 = normalize(icao24)

        def score(candidate: AerodataboxFlight) -> int:
            current = 0
            if normalize(candidate.callSign) == callsign:
                current += 40
            if normalize(candidate.number) == callsign:
                current += 20
            if candidate.aircraft and normalize(candidate.aircraft.modeS) == normalized_icao24:
                current += 50
            if candidate.location and candidate.location.lat is not None and candidate.location.lon is not None:
                current += 10
            return current

        return max(live_flights, key=score)

    @staticmethod
    def _select_matching_db_flight(
        db_flights: Sequence[Flight],
        live_flight: AerodataboxFlight,
        icao24: str | None,
    ) -> Flight:
        def normalize(value: str | None) -> str:
            return (value or "").strip().replace(" ", "").upper()

        normalized_icao24 = normalize(icao24)

        def score(candidate: Flight) -> int:
            current = 0
            if normalize(candidate.number) == normalize(live_flight.number):
                current += 20
            if live_flight.aircraft and normalize(candidate.aircraft_modeS) == normalize(live_flight.aircraft.modeS):
                current += 30
            if normalized_icao24 and normalize(candidate.aircraft_modeS) == normalized_icao24:
                current += 40
            return current

        return max(db_flights, key=score)

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

    @staticmethod
    async def get_copilot_telemetry(flight: Flight) -> CopilotTelemetryRead:
        try:
            api_client = AerodataboxClient()
            live_flights = await api_client.get_flight(
                full_number=flight.number,
                departure_date=flight.date,
                with_location=True,
            )
            live_flight = FlightService._select_matching_live_flight(
                db_flight=flight,
                live_flights=live_flights,
            )
        except Exception:
            live_flight = None

        return FlightService._build_copilot_telemetry_response(
            db_flight=flight,
            live_flight=live_flight,
        )

    @staticmethod
    def _select_matching_live_flight(
        db_flight: Flight, live_flights: list[AerodataboxFlight]
    ) -> AerodataboxFlight | None:
        if not live_flights:
            return None

        def normalize(value: str | None) -> str:
            return (value or "").strip().replace(" ", "").upper()

        def score(candidate: AerodataboxFlight) -> int:
            current = 0

            if normalize(candidate.number) == normalize(db_flight.number):
                current += 10

            if db_flight.departure and db_flight.departure.airport:
                if normalize(candidate.departure.airport.iata) == normalize(
                    db_flight.departure.airport.iata
                ):
                    current += 20

                if candidate.departure.scheduledTime:
                    if candidate.departure.scheduledTime.utc == db_flight.departure.scheduled_time_utc:
                        current += 20
                    if candidate.departure.scheduledTime.local == db_flight.departure.scheduled_time_local:
                        current += 20

            if db_flight.arrival and db_flight.arrival.airport:
                if normalize(candidate.arrival.airport.iata) == normalize(
                    db_flight.arrival.airport.iata
                ):
                    current += 10

            return current

        return max(live_flights, key=score)

    @staticmethod
    def _build_copilot_telemetry_response(
        db_flight: Flight,
        live_flight: AerodataboxFlight | None,
    ) -> CopilotTelemetryRead:
        location = live_flight.location if live_flight else None
        location_available = FlightService._location_has_position(location)

        return CopilotTelemetryRead(
            flight_id=db_flight.id or 0,
            flight_number=db_flight.number,
            status=(
                live_flight.status.value
                if live_flight and isinstance(live_flight.status, FlightStatusEnum)
                else str(live_flight.status if live_flight else db_flight.status)
            ),
            data_source="live" if location_available else "saved",
            location_available=location_available,
            reported_at_utc=location.reportedAtUtc if location else None,
            lat=location.lat if location else None,
            lon=location.lon if location else None,
            altitude_feet=(
                location.altitude.feet
                if location and location.altitude
                else None
            ),
            altitude_meter=(
                location.altitude.meter
                if location and location.altitude
                else None
            ),
            pressure_altitude_feet=(
                location.pressureAltitude.feet
                if location and location.pressureAltitude
                else None
            ),
            pressure_altitude_meter=(
                location.pressureAltitude.meter
                if location and location.pressureAltitude
                else None
            ),
            ground_speed_mph=(
                location.groundSpeed.miPerHour
                if location and location.groundSpeed
                else None
            ),
            ground_speed_kph=(
                location.groundSpeed.kmPerHour
                if location and location.groundSpeed
                else None
            ),
            ground_speed_kt=(
                location.groundSpeed.kt
                if location and location.groundSpeed
                else None
            ),
            true_track_deg=(
                location.trueTrack.deg
                if location and location.trueTrack
                else None
            ),
            vertical_speed_fpm=location.vsiFpm if location else None,
        )

    @staticmethod
    def _location_has_position(location: AerodataboxFlightLocation | None) -> bool:
        return bool(location and location.lat is not None and location.lon is not None)


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
        if not flights:
            try:
                resolved_flight = await FlightService.resolve_global_live_flight(
                    session=session,
                    callsign=f"{airline_iata}{flight_number}",
                )
                if resolved_flight:
                    flights = [resolved_flight]
            except Exception:
                logger.exception(
                    f"Unable to resolve live fallback for airline_iata={airline_iata}, flight_number={flight_number}"
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
