import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from fastapi import HTTPException, status
from sqlmodel import Session, select

from ...config import settings
from ...models.aerodatabox import (
    AerodataboxFlight,
    AerodataboxFlightLocation,
    AirportFidsContract,
    FlightStatusEnum,
)
from ...models.flight import (
    Airline,
    AirportFlightOriginAndDestinationInfoRead,
    AirportFlightRead,
    CopilotTelemetryRead,
    Flight,
    FlightRead,
    GlobalFlightResolveCandidate,
    GlobalFlightPositionRead,
    QuerySearchResponse,
)
from .api_client import (
    ADSBExchangeRateLimitedError,
    ADSBExchangeClient,
    AerodataboxClient,
    AerodataboxUnavailableError,
)
from .mapper import AirportFlightMapper
from .persistence import FlightPersistence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormalizedGlobalFlightResolveCandidate:
    callsign: str
    icao24: str | None = None


@dataclass(frozen=True)
class GlobalLiveFlightResolveMatch:
    candidate: NormalizedGlobalFlightResolveCandidate
    departure_date: str
    live_flights: list[AerodataboxFlight]
    live_flight: AerodataboxFlight


@dataclass(frozen=True)
class LiveRegistrationSearchResult:
    flight: Flight | None
    failure_reason: str | None
    provider_result_count: int


class FlightService:
    _global_positions_cache: tuple[datetime, list[GlobalFlightPositionRead]] | None = None
    _iata_by_icao_cache: dict[str, str] | None = None
    _iata_by_icao_overrides = {
        "DLH": "LH",
        "GLO": "G3",
        "SIA": "SQ",
    }
    _route_callsign_patterns = (
        re.compile(r"^([A-Z]{3})([0-9]{1,4})$"),
        re.compile(r"^([A-Z0-9]{2})([0-9]{1,4})$"),
    )
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
        positions = await FlightService._get_cached_global_positions(now=now)

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
    async def _get_cached_global_positions(
        now: datetime | None = None,
    ) -> list[GlobalFlightPositionRead]:
        now = now or datetime.now(timezone.utc)
        cached = FlightService._global_positions_cache
        ttl_seconds = max(1, settings.ADSBEXCHANGE_CACHE_TTL_SECONDS)
        if cached and now - cached[0] < timedelta(seconds=ttl_seconds):
            return cached[1]

        try:
            positions = await ADSBExchangeClient().get_global_positions()
            FlightService._global_positions_cache = (now, positions)
            return positions
        except Exception:
            logger.exception("Unable to fetch global flight positions")
            return cached[1] if cached else []

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
        major_airlines_only: bool | None = None,
    ) -> bool:
        if position.on_ground:
            return False

        if not FlightService._looks_like_route_callsign(position):
            return False

        continent = FlightService._continent_key(lat=position.lat, lon=position.lon)
        if major_airlines_only is None:
            major_airlines_only = settings.GLOBAL_FLIGHT_MAJOR_AIRLINES_ONLY

        if (
            major_airlines_only
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
    async def get_airline_live_flights(
        session: Session,
        airline_iata: str,
        limit: int = 8,
    ) -> list[Flight]:
        airline_iata = airline_iata.strip().upper()
        if not airline_iata or limit <= 0:
            return []

        now = datetime.now(timezone.utc)
        now_timestamp = int(now.timestamp())
        positions = await FlightService._get_cached_global_positions(now=now)

        candidates: list[GlobalFlightPositionRead] = []
        for position in positions:
            if not FlightService._global_position_matches_airline(
                position=position,
                airline_iata=airline_iata,
            ):
                continue

            if FlightService._global_position_is_on_route(
                position=position,
                now_timestamp=now_timestamp,
                major_airlines_only=False,
            ):
                candidates.append(position)

        flights: list[Flight] = []
        seen_flight_numbers: set[str] = set()
        for position in FlightService._sample_global_positions(candidates, limit * 2):
            callsign = (position.callsign or position.display_code or "").strip().upper()
            if not callsign or callsign in seen_flight_numbers:
                continue

            try:
                flight = await FlightService.resolve_global_live_flight(
                    session=session,
                    callsign=callsign,
                    icao24=position.icao24,
                )
            except Exception:
                logger.exception(
                    "Unable to resolve airline live flight airline_iata=%s, callsign=%s",
                    airline_iata,
                    callsign,
                )
                continue

            if not flight:
                continue

            seen_flight_numbers.add(callsign)
            flights.append(flight)
            if len(flights) >= limit:
                break

        return flights

    @staticmethod
    def _global_position_matches_airline(
        position: GlobalFlightPositionRead,
        airline_iata: str,
    ) -> bool:
        prefix = FlightService._callsign_airline_prefix(position)
        if not prefix:
            return False

        normalized_prefix = FlightService._iata_for_airline_prefix(prefix) or prefix
        return normalized_prefix == airline_iata or prefix == airline_iata

    @staticmethod
    def _looks_like_route_callsign(position: GlobalFlightPositionRead) -> bool:
        code = (position.callsign or position.display_code or "").strip().upper()
        if not code or code == position.icao24.upper():
            return False

        if len(code) < 4 or len(code) > 8:
            return False

        if code.startswith("N") and len(code) > 1 and code[1].isdigit():
            return False

        return FlightService._route_callsign_match(code) is not None

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
        match = FlightService._route_callsign_match(code)
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
        match = FlightService._route_callsign_match(code)
        if not match:
            return None

        prefix, number = match.groups()
        iata = FlightService._iata_for_airline_prefix(prefix)
        if not iata:
            return code

        return f"{iata}{number}"

    @staticmethod
    def _route_callsign_match(code: str) -> re.Match[str] | None:
        for pattern in FlightService._route_callsign_patterns:
            match = pattern.match(code)
            if match:
                return match

        return None

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
        candidate = FlightService._normalized_global_resolve_candidate(
            callsign=callsign,
            icao24=icao24,
        )
        if not candidate:
            return None

        match = await FlightService._fetch_global_live_flight_match_for_candidate(
            candidate=candidate,
            require_on_route=False,
        )
        if not match:
            return None

        return FlightService._persist_global_live_flight_match(
            session=session,
            match=match,
        )

    @staticmethod
    async def resolve_live_aircraft_registration(
        session: Session,
        registration: str,
    ) -> LiveRegistrationSearchResult:
        try:
            positions = await ADSBExchangeClient().get_positions_for_registration(
                registration=registration,
            )
        except ADSBExchangeRateLimitedError:
            return LiveRegistrationSearchResult(
                flight=None,
                failure_reason="provider_rate_limited",
                provider_result_count=0,
            )
        if positions is None:
            return LiveRegistrationSearchResult(
                flight=None,
                failure_reason="provider_unavailable",
                provider_result_count=0,
            )
        if not positions:
            return LiveRegistrationSearchResult(
                flight=None,
                failure_reason="registration_not_live",
                provider_result_count=0,
            )

        for position in positions:
            callsign = (position.callsign or "").strip()
            if len(callsign) < 3:
                continue

            flight = await FlightService.resolve_global_live_flight(
                session=session,
                callsign=callsign,
                icao24=position.icao24,
            )
            if flight:
                return LiveRegistrationSearchResult(
                    flight=flight,
                    failure_reason=None,
                    provider_result_count=len(positions),
                )

        return LiveRegistrationSearchResult(
            flight=None,
            failure_reason="registration_live_unresolved",
            provider_result_count=len(positions),
        )

    @staticmethod
    async def resolve_global_live_flight_candidates(
        session: Session,
        candidates: Sequence[GlobalFlightResolveCandidate],
    ) -> Flight | None:
        normalized_candidates = FlightService._normalized_global_resolve_candidates(
            candidates=candidates,
            limit=settings.GLOBAL_FLIGHT_RESOLVE_CANDIDATE_LIMIT,
        )
        if not normalized_candidates:
            return None

        tasks = [
            asyncio.create_task(
                FlightService._fetch_global_live_flight_match_for_candidate(
                    candidate=candidate,
                    require_on_route=True,
                )
            )
            for candidate in normalized_candidates
        ]

        try:
            original_match = await tasks[0]
            if original_match:
                original_flight = FlightService._safe_persist_global_live_flight_match(
                    session=session,
                    match=original_match,
                )
                if original_flight:
                    await FlightService._cancel_global_resolve_tasks(tasks[1:])
                    return original_flight

            for task in asyncio.as_completed(tasks[1:]):
                match = await task
                if not match:
                    continue

                backup_flight = FlightService._safe_persist_global_live_flight_match(
                    session=session,
                    match=match,
                )
                if backup_flight:
                    await FlightService._cancel_global_resolve_tasks(tasks)
                    return backup_flight

            return None
        finally:
            await FlightService._cancel_global_resolve_tasks(tasks)

    @staticmethod
    async def _fetch_global_live_flight_match_for_candidate(
        candidate: NormalizedGlobalFlightResolveCandidate,
        require_on_route: bool,
    ) -> GlobalLiveFlightResolveMatch | None:
        timeout = max(1.0, settings.GLOBAL_FLIGHT_RESOLVE_CANDIDATE_TIMEOUT_SECONDS)
        try:
            return await asyncio.wait_for(
                FlightService._fetch_global_live_flight_match(
                    candidate=candidate,
                    require_on_route=require_on_route,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning(
                "Timed out resolving global live flight callsign=%s, icao24=%s",
                candidate.callsign,
                candidate.icao24,
            )
        except Exception:
            logger.exception(
                "Unable to resolve global live flight candidate callsign=%s, icao24=%s",
                candidate.callsign,
                candidate.icao24,
            )

        return None

    @staticmethod
    async def _fetch_global_live_flight_match(
        candidate: NormalizedGlobalFlightResolveCandidate,
        require_on_route: bool,
    ) -> GlobalLiveFlightResolveMatch | None:
        today = datetime.now(timezone.utc).date()
        dates = [
            today.isoformat(),
            (today - timedelta(days=1)).isoformat(),
            (today + timedelta(days=1)).isoformat(),
        ]

        api_client = AerodataboxClient()
        for departure_date in dates:
            live_flights = await api_client.get_flight(
                full_number=candidate.callsign,
                departure_date=departure_date,
                with_location=True,
            )
            if not live_flights:
                continue

            selectable_live_flights = live_flights
            if require_on_route:
                selectable_live_flights = [
                    live_flight
                    for live_flight in live_flights
                    if FlightService._aerodatabox_live_flight_is_on_route(live_flight)
                ]
                if not selectable_live_flights:
                    continue

            live_flight = FlightService._select_matching_global_live_flight(
                live_flights=selectable_live_flights,
                callsign=candidate.callsign,
                icao24=candidate.icao24,
            )

            return GlobalLiveFlightResolveMatch(
                candidate=candidate,
                departure_date=departure_date,
                live_flights=live_flights,
                live_flight=live_flight,
            )

        return None

    @staticmethod
    def _persist_global_live_flight_match(
        session: Session,
        match: GlobalLiveFlightResolveMatch,
    ) -> Flight | None:
        normalized_number = match.live_flight.number.strip().replace(" ", "").upper()

        db_flights = FlightPersistence.get_flights(
            session=session,
            full_number=normalized_number,
            departure_date=match.departure_date,
        )
        if db_flights:
            return FlightService._select_matching_db_flight(
                db_flights=db_flights,
                live_flight=match.live_flight,
                icao24=match.candidate.icao24,
            )

        created = FlightPersistence.create_flights_from_aerodatabox_model(
            flights=match.live_flights,
            airline_iata=(
                match.live_flight.airline.iata
                if match.live_flight.airline and match.live_flight.airline.iata
                else ""
            ),
            departure_date=match.departure_date,
            session=session,
        )
        if created:
            return FlightService._select_matching_db_flight(
                db_flights=created,
                live_flight=match.live_flight,
                icao24=match.candidate.icao24,
            )

        return None

    @staticmethod
    def _safe_persist_global_live_flight_match(
        session: Session,
        match: GlobalLiveFlightResolveMatch,
    ) -> Flight | None:
        try:
            return FlightService._persist_global_live_flight_match(
                session=session,
                match=match,
            )
        except Exception:
            session.rollback()
            logger.exception(
                "Unable to persist global live flight callsign=%s, icao24=%s",
                match.candidate.callsign,
                match.candidate.icao24,
            )
            return None

    @staticmethod
    async def _cancel_global_resolve_tasks(tasks: Sequence[asyncio.Task]) -> None:
        pending_tasks = [task for task in tasks if not task.done()]
        for task in pending_tasks:
            task.cancel()

        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

    @staticmethod
    def _normalized_global_resolve_candidates(
        candidates: Sequence[GlobalFlightResolveCandidate],
        limit: int,
    ) -> list[NormalizedGlobalFlightResolveCandidate]:
        normalized_candidates: list[NormalizedGlobalFlightResolveCandidate] = []
        seen: set[tuple[str, str | None]] = set()
        for candidate in candidates:
            normalized = FlightService._normalized_global_resolve_candidate(
                callsign=candidate.callsign,
                icao24=candidate.icao24,
            )
            if not normalized:
                continue

            key = (normalized.callsign, normalized.icao24)
            if key in seen:
                continue

            seen.add(key)
            normalized_candidates.append(normalized)
            if len(normalized_candidates) >= limit:
                break

        return normalized_candidates

    @staticmethod
    def _normalized_global_resolve_candidate(
        callsign: str,
        icao24: str | None,
    ) -> NormalizedGlobalFlightResolveCandidate | None:
        normalized_callsign = callsign.strip().replace(" ", "").upper()
        if len(normalized_callsign) < 3:
            return None

        normalized_icao24 = (icao24 or "").strip().lower() or None
        return NormalizedGlobalFlightResolveCandidate(
            callsign=normalized_callsign,
            icao24=normalized_icao24,
        )

    @staticmethod
    def _aerodatabox_live_flight_is_on_route(live_flight: AerodataboxFlight) -> bool:
        return live_flight.status not in {
            FlightStatusEnum.ARRIVED,
            FlightStatusEnum.CANCELED,
            FlightStatusEnum.CANCELEDUNCERTAIN,
            FlightStatusEnum.DIVERTED,
        }

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
        normalized_airline = airline_iata.strip().replace(" ", "").upper()
        normalized_number = flight_number.strip().replace(" ", "").upper()
        full_number = f"{normalized_airline}{normalized_number}"

        db_flights = FlightPersistence.get_flights(session, full_number, departure_date)
        if db_flights:
            return db_flights

        api_client = AerodataboxClient()

        flights = await api_client.get_flight(
            full_number=full_number, departure_date=departure_date
        )
        if not flights:
            return []

        return FlightPersistence.create_flights_from_aerodatabox_model(
            flights=flights,
            airline_iata=normalized_airline,
            departure_date=departure_date,
            session=session,
        )

    @staticmethod
    def _flight_number_candidates(
        *, airline_iata: str, flight_number: str
    ) -> tuple[str, ...]:
        airline = airline_iata.strip().replace(" ", "").upper()
        supplied = flight_number.strip().replace(" ", "").upper()
        candidates = [supplied]

        # A client may send the complete designator in the flight-number field.
        if airline and supplied.startswith(airline) and len(supplied) > len(airline):
            candidates.append(supplied[len(airline):])

        # Affected app builds retained the numeric character from codes such as
        # B6, W6 and U2 when they extracted the number suffix.
        if (
            len(airline) == 2
            and airline[0].isalpha()
            and airline[1].isdigit()
            and supplied.startswith(airline[1])
            and len(supplied) > 1
        ):
            candidates.append(supplied[1:])

        return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))

    @staticmethod
    async def get_airport_flights(
        airport_iata: str, departure_date: str, direction: str = "Both"
    ) -> AirportFidsContract:
        """
        Get flights for an airport for the window of 24H from Aerodatabox API.
        """
        api_client = AerodataboxClient()

        try:
            return await api_client.get_airport_flights(
                airport_iata=airport_iata,
                departure_date=departure_date,
                direction=direction,
            )
        except AerodataboxUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Flight data provider temporarily unavailable",
            ) from exc

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
    SEARCH_STATUS_PRIORITY = {
        FlightStatusEnum.ENROUTE.value: 0,
        FlightStatusEnum.DEPARTED.value: 1,
        FlightStatusEnum.APPROACHING.value: 2,
        FlightStatusEnum.DIVERTED.value: 3,
        FlightStatusEnum.BOARDING.value: 4,
        FlightStatusEnum.GATECLOSED.value: 5,
        FlightStatusEnum.CHECKIN.value: 6,
        FlightStatusEnum.DELAYED.value: 7,
        FlightStatusEnum.EXPECTED.value: 8,
        FlightStatusEnum.UNKNOWN.value: 20,
        FlightStatusEnum.CANCELEDUNCERTAIN.value: 30,
        FlightStatusEnum.CANCELED.value: 31,
        FlightStatusEnum.ARRIVED.value: 40,
    }
    TRACKABLE_SEARCH_STATUSES = {
        FlightStatusEnum.EXPECTED.value,
        FlightStatusEnum.CHECKIN.value,
        FlightStatusEnum.BOARDING.value,
        FlightStatusEnum.GATECLOSED.value,
        FlightStatusEnum.DELAYED.value,
        FlightStatusEnum.ENROUTE.value,
        FlightStatusEnum.DEPARTED.value,
        FlightStatusEnum.APPROACHING.value,
        FlightStatusEnum.DIVERTED.value,
    }
    ACTIVE_SEARCH_STATUSES = {
        FlightStatusEnum.ENROUTE.value,
        FlightStatusEnum.DEPARTED.value,
        FlightStatusEnum.APPROACHING.value,
        FlightStatusEnum.DIVERTED.value,
    }

    @staticmethod
    async def extract_random_flight(
        random: bool,
        session: Session
    ):
        candidates = FlightPersistence.get_random_flights(session=session, limit=100)
        flight = next(
            (
                candidate
                for candidate in candidates
                if FlightQueryHandler.is_trackable_search_flight(candidate)
            ),
            None,
        )
        return QuerySearchResponse(
            flights_result=[FlightRead.model_validate(flight, from_attributes=True)] if flight else []
        )

    @classmethod
    def filter_trackable_search_response(
        cls,
        response: QuerySearchResponse,
        *,
        now: datetime | None = None,
    ) -> int:
        """Mirror the released onboarding's active/upcoming flight filter."""
        effective_now = now or datetime.now(timezone.utc)
        original_count = len(response.flights_result) + len(
            response.airport_flights_result
        )
        response.flights_result = [
            flight
            for flight in response.flights_result
            if cls.is_trackable_search_flight(flight, now=effective_now)
        ]
        response.airport_flights_result = [
            flight
            for flight in response.airport_flights_result
            if cls.is_trackable_search_flight(flight, now=effective_now)
        ]
        return original_count - (
            len(response.flights_result) + len(response.airport_flights_result)
        )

    @classmethod
    def trackable_search_result_count(
        cls,
        response: QuerySearchResponse,
        *,
        now: datetime | None = None,
    ) -> int:
        effective_now = now or datetime.now(timezone.utc)
        return sum(
            cls.is_trackable_search_flight(flight, now=effective_now)
            for flight in response.flights_result
        ) + sum(
            cls.is_trackable_search_flight(flight, now=effective_now)
            for flight in response.airport_flights_result
        )

    @classmethod
    def search_response_is_landed_only(cls, response: QuerySearchResponse) -> bool:
        flights = [*response.flights_result, *response.airport_flights_result]
        return bool(flights) and all(
            cls._search_status_value(flight.status) == FlightStatusEnum.ARRIVED.value
            for flight in flights
        )

    @classmethod
    def is_trackable_search_flight(
        cls,
        flight: FlightRead | AirportFlightRead | Flight,
        *,
        now: datetime | None = None,
    ) -> bool:
        effective_now = now or datetime.now(timezone.utc)
        status_value = cls._search_status_value(flight.status)
        if status_value not in cls.TRACKABLE_SEARCH_STATUSES:
            return False

        arrival = getattr(flight, "arrival", None)
        departure = getattr(flight, "departure", None)
        actual_arrival = cls._search_segment_time(arrival, "runway_time_utc")
        if actual_arrival and actual_arrival <= effective_now:
            return False

        expected_arrival = actual_arrival or cls._first_search_segment_time(
            arrival,
            "revised_time_utc",
            "predicted_time_utc",
            "scheduled_time_utc",
        )
        if expected_arrival:
            return expected_arrival > effective_now

        effective_departure = cls._first_search_segment_time(
            departure,
            "runway_time_utc",
            "revised_time_utc",
            "predicted_time_utc",
            "scheduled_time_utc",
        )
        if effective_departure:
            return (
                effective_departure > effective_now
                or status_value in cls.ACTIVE_SEARCH_STATUSES
            )

        return status_value in cls.ACTIVE_SEARCH_STATUSES

    @staticmethod
    def _search_status_value(status_value: Any) -> str:
        return str(getattr(status_value, "value", status_value))

    @classmethod
    def _first_search_segment_time(
        cls,
        segment: Any,
        *fields: str,
    ) -> datetime | None:
        for field in fields:
            parsed = cls._search_segment_time(segment, field)
            if parsed:
                return parsed
        return None

    @staticmethod
    def _search_segment_time(segment: Any, field: str) -> datetime | None:
        value = getattr(segment, field, None) if segment else None
        if not value:
            return None

        normalized = str(value).strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    async def extract_airline_live_flights(
        airline_iata: str,
        departure_date: str,
        session: Session,
        limit: int = 8,
        **kwargs,
    ):
        normalized_airline_iata = airline_iata.strip().upper()
        flights = await FlightService.get_airline_live_flights(
            session=session,
            airline_iata=normalized_airline_iata,
            limit=limit,
        )

        if not flights:
            flights = session.exec(
                select(Flight)
                .join(Airline, Flight.airline_id == Airline.id)
                .where(Airline.iata == normalized_airline_iata)
                .where(Flight.date >= departure_date)
                .limit(limit)
            ).all()

        flight_reads = [
            FlightRead.model_validate(flight, from_attributes=True)
            for flight in flights
        ]
        return QuerySearchResponse(
            flights_result=FlightQueryHandler._rank_exact_flights(
                flight_reads,
                requested_number="",
            )
        )

    @staticmethod
    async def extract_flight_info(
        departure_date: str,
        flight_number: str,
        airline_iata: str,
        session: Session,
        allow_live_fallback: bool = True,
    ):
        flights = await FlightService.get_flights(
            session=session,
            departure_date=departure_date,
            flight_number=flight_number,
            airline_iata=airline_iata,
        )
        if not flights and allow_live_fallback:
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

        flight_reads = [
            FlightRead.model_validate(flight, from_attributes=True)
            for flight in flights
        ]
        return QuerySearchResponse(
            flights_result=FlightQueryHandler._rank_exact_flights(
                flight_reads,
                requested_number=f"{airline_iata}{flight_number}",
            )
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
        airline_iata: str | None = None,
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
            if airline_iata:
                requested_airline = airline_iata.upper()
                provider_airline = (
                    flight.airline.iata.upper()
                    if flight.airline and flight.airline.iata
                    else ""
                )
                provider_number = re.sub(r"\s+", "", flight.number.upper())
                if (
                    provider_airline != requested_airline
                    and not provider_number.startswith(requested_airline)
                ):
                    continue
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

        return sorted(
            best_by_key.values(),
            key=FlightQueryHandler._airport_search_sort_key,
        )

    @staticmethod
    def _airport_flight_key(
        flight: AirportFlightRead,
    ) -> tuple[str, str, str, str, str]:
        departure_time = (
            flight.departure.scheduled_time_utc if flight.departure else ""
        ) or ""
        arrival_time = (
            flight.arrival.scheduled_time_utc if flight.arrival else ""
        ) or ""
        # Identical route and scheduled timestamps are the stable signal shared
        # by marketing/operating codeshares. When timing is incomplete, retain
        # the number so unrelated flights are never collapsed.
        incomplete_discriminator = (
            flight.number.strip().replace(" ", "").upper()
            if not departure_time or not arrival_time
            else ""
        )
        return (
            (flight.departure.airport.iata if flight.departure else "") or "",
            (flight.arrival.airport.iata if flight.arrival else "") or "",
            departure_time,
            arrival_time,
            incomplete_discriminator,
        )

    @staticmethod
    def _airport_flight_score(flight: AirportFlightRead) -> int:
        status_value = (
            flight.status.value if isinstance(flight.status, FlightStatusEnum)
            else str(flight.status)
        )
        priority = FlightQueryHandler.SEARCH_STATUS_PRIORITY.get(status_value, 20)
        score = 1000 - (priority * 20)

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
    def _airport_search_sort_key(
        flight: AirportFlightRead,
    ) -> tuple[int, str, str]:
        status_value = (
            flight.status.value if isinstance(flight.status, FlightStatusEnum)
            else str(flight.status)
        )
        scheduled = (
            flight.departure.scheduled_time_utc if flight.departure else ""
        ) or "9999"
        return (
            FlightQueryHandler.SEARCH_STATUS_PRIORITY.get(status_value, 20),
            scheduled,
            flight.number,
        )

    @staticmethod
    def _rank_exact_flights(
        flights: list[FlightRead],
        requested_number: str,
    ) -> list[FlightRead]:
        requested = requested_number.strip().replace(" ", "").upper()
        best_by_physical_flight: dict[
            tuple[str, str, str, str, str],
            FlightRead,
        ] = {}

        for flight in flights:
            key = FlightQueryHandler._direct_physical_flight_key(flight)
            current = best_by_physical_flight.get(key)
            if current is None or FlightQueryHandler._direct_flight_score(
                flight,
                requested,
            ) > FlightQueryHandler._direct_flight_score(current, requested):
                best_by_physical_flight[key] = flight

        return sorted(
            best_by_physical_flight.values(),
            key=lambda flight: FlightQueryHandler._direct_flight_sort_key(
                flight,
                requested,
            ),
        )

    @staticmethod
    def _direct_physical_flight_key(
        flight: FlightRead,
    ) -> tuple[str, str, str, str, str]:
        departure_iata = (
            flight.departure.airport.iata if flight.departure else ""
        ) or ""
        arrival_iata = (
            flight.arrival.airport.iata if flight.arrival else ""
        ) or ""
        departure_time = (
            flight.departure.scheduled_time_utc if flight.departure else ""
        ) or ""
        arrival_time = (
            flight.arrival.scheduled_time_utc if flight.arrival else ""
        ) or ""
        discriminator = (
            ""
            if departure_iata and arrival_iata and departure_time and arrival_time
            else flight.number.strip().replace(" ", "").upper()
        )
        return (
            departure_iata,
            arrival_iata,
            departure_time,
            arrival_time,
            discriminator,
        )

    @staticmethod
    def _direct_flight_score(flight: FlightRead, requested: str) -> int:
        number = flight.number.strip().replace(" ", "").upper()
        status_value = (
            flight.status.value if isinstance(flight.status, FlightStatusEnum)
            else str(flight.status)
        )
        priority = FlightQueryHandler.SEARCH_STATUS_PRIORITY.get(status_value, 20)
        score = 10000 if number == requested else 0
        score += 1000 - (priority * 20)

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
    def _direct_flight_sort_key(
        flight: FlightRead,
        requested: str,
    ) -> tuple[int, int, str, str]:
        number = flight.number.strip().replace(" ", "").upper()
        status_value = (
            flight.status.value if isinstance(flight.status, FlightStatusEnum)
            else str(flight.status)
        )
        scheduled = (
            flight.departure.scheduled_time_utc if flight.departure else ""
        ) or "9999"
        return (
            0 if number == requested else 1,
            FlightQueryHandler.SEARCH_STATUS_PRIORITY.get(status_value, 20),
            scheduled,
            number,
        )

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
