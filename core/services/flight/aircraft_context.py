"""Public aircraft trace and strictly aircraft-matched route, no user assignment.

Trace contract: gateway.adsbexchange.com/api/aircraft/v2/docs/openapi.json
Only observed current-leg samples are returned. Gaps are never interpolated.
"""
import asyncio
import json
import math
import re
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone

import httpx
from pydantic import PrivateAttr
from sqlmodel import SQLModel, Field

from ...config import settings
from .api_client import ADSBExchangeClient, AerodataboxClient
from .aircraft_metadata import visible_aircraft
from .schedule_pool import SchedulePool
from ...models.flight import GlobalFlightPositionRead


class SelectedAircraftPositions:
    """Separate, bounded per-worker cache and upstream budget for active Copilot."""
    cache: OrderedDict[str, tuple[float, GlobalFlightPositionRead | None]] = OrderedDict()
    in_flight = False
    next_request_at = 0.0

    @classmethod
    async def get(cls, icao24: str) -> GlobalFlightPositionRead | None:
        now = time.monotonic()
        cached = cls.cache.get(icao24)
        if cached and now - cached[0] < 15:
            return cached[1]
        if cls.in_flight or now < cls.next_request_at:
            return None
        cls.in_flight = True
        cls.next_request_at = now + 2
        client = ADSBExchangeClient()
        result = None
        try:
            async with asyncio.timeout(8):
                positions = await client.get_positions_for_path(f"hex/{icao24}", raise_for_status=True)
            timestamp = int(time.time())
            result = max((position for position in positions
                          if position.icao24 == icao24 and visible_aircraft(position, timestamp)),
                         key=lambda position: position.time_position, default=None)
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {401, 402, 403, 429}:
                cls.next_request_at = time.monotonic() + 60
        except (httpx.HTTPError, TimeoutError, ValueError):
            pass
        finally:
            try:
                await client.client.aclose()
            finally:
                cls.in_flight = False
        cls.cache[icao24] = (time.monotonic(), result)
        cls.cache.move_to_end(icao24)
        while len(cls.cache) > 256:
            cls.cache.popitem(last=False)
        return result


class AircraftTrailPoint(SQLModel):
    timestamp: int
    lat: float
    lon: float


class AircraftRouteEndpoint(SQLModel):
    code: str | None = None
    name: str | None = None
    city: str | None = None
    scheduled_time_local: str | None = None


class AircraftContext(SQLModel):
    _schedule_valid_until: float | None = PrivateAttr(default=None)
    icao24: str
    segments: list[list[AircraftTrailPoint]] = Field(default_factory=list)
    trail_status: str = "unavailable"
    trail_source: str | None = None
    origin: AircraftRouteEndpoint | None = None
    destination: AircraftRouteEndpoint | None = None
    route_status: str = "unavailable"


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def parse_trace(payload: dict, icao24: str, now: int) -> list[list[AircraftTrailPoint]]:
    if str(payload.get("icao", "")).lower() != icao24:
        return []
    base = finite(payload.get("timestamp"))
    rows = payload.get("trace")
    if base is None or not isinstance(rows, list) or len(rows) > 100000:
        return []
    segments, current = [], []
    previous = None
    last_timestamp = -1
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            if current: segments.append(current)
            current, previous = [], None
            continue
        offset, lat, lon = (finite(row[i]) for i in range(3))
        flags = row[6] if type(row[6]) is int and row[6] >= 0 else 1
        if offset is None or lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            if current: segments.append(current)
            current, previous = [], None
            continue
        timestamp = int(base + offset)
        if not now - 21600 <= timestamp <= now + 30:
            if current: segments.append(current)
            current, previous = [], None
            continue
        if timestamp <= last_timestamp:
            continue
        last_timestamp = timestamp
        # A chronological new leg/ground observation ends the prior flight.
        if flags & 2 or row[3] == "ground":
            segments, current, previous = [], [], None
        if flags & 1 or row[3] == "ground" or not now - 21600 <= timestamp <= now + 30:
            if current: segments.append(current)
            current, previous = [], None
            continue
        point = AircraftTrailPoint(timestamp=timestamp, lat=lat, lon=lon)
        if previous and timestamp <= previous.timestamp:
            continue  # duplicate/out-of-order samples never create a backwards line
        # Long gaps and physically implausible jumps split, never bridge.
        if previous:
            dt = timestamp - previous.timestamp
            lat1, lat2 = math.radians(previous.lat), math.radians(lat)
            dlon = math.radians(lon - previous.lon)
            a = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
            km = 6371 * 2 * math.asin(math.sqrt(min(1, max(0, a))))
            if dt > 600 or km / dt > 2:
                if current: segments.append(current)
                current = []
        current.append(point)
        previous = point
    if current: segments.append(current)
    # Bound output without connecting distinct segments. Keep both endpoints.
    result = []
    for segment in segments[-16:]:
        if len(segment) > 128:
            segment = [segment[round(i * (len(segment)-1) / 127)] for i in range(128)]
        if len(segment) > 1:
            result.append(segment)
    return result


def matched_route(flight, position, now: datetime, *, resolver_number=None):
    aircraft = flight.aircraft
    if not aircraft:
        return None
    normalize = lambda value: re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    if aircraft.modeS:
        identity_matches = aircraft.modeS.strip().lower() == position.icao24.strip().lower()
    else:
        identity_matches = bool(position.registration) and normalize(aircraft.reg) == normalize(position.registration)
    if not identity_matches:
        return None
    resolver_identity = bool(
        resolver_number and aircraft.modeS and position.callsign
        and normalize(resolver_number) == normalize(getattr(flight, "number", None))
        and (not getattr(flight, "callSign", None) or normalize(flight.callSign) in {
            normalize(position.callsign), normalize(resolver_number)
        })
        and not position.aircraft_categories and getattr(flight, "isCargo", None) is False
    )
    status = str(flight.status.value)
    live_status = status in {"EnRoute", "Departed", "Approaching", "Delayed"}
    # ADS-B has already confirmed this exact passenger aircraft is airborne.
    # Expected is a lagging schedule status, never evidence of a live state.
    if not live_status and not (status == "Expected" and resolver_identity
                                and visible_aircraft(position, int(now.timestamp()))):
        return None
    try:
        updated = datetime.fromisoformat(flight.lastUpdatedUtc.replace("Z", "+00:00"))
        if updated.tzinfo is None or (now - updated).total_seconds() < -30:
            return None
    except (ValueError, TypeError):
        return None
    location = flight.location
    location_fresh = False
    if location is not None:
        if (location.lat is None or location.lon is None or not location.reportedAtUtc) and resolver_identity:
            # Incomplete telemetry is not evidence of a contradictory position.
            location = None
    if location is not None:
        if location.lat is None or location.lon is None or not location.reportedAtUtc:
            return None
        if not -90 <= location.lat <= 90 or not -180 <= location.lon <= 180:
            return None
        try:
            reported = datetime.fromisoformat(location.reportedAtUtc.replace("Z", "+00:00"))
            # The field itself is explicitly UTC; some provider rows omit Z.
            if reported.tzinfo is None:
                reported = reported.replace(tzinfo=timezone.utc)
            if (now - reported).total_seconds() < -30:
                return None
            location_fresh = (now - reported).total_seconds() <= 900
            if location_fresh:
                lat1, lat2 = math.radians(position.lat), math.radians(location.lat)
                dlon = math.radians(position.lon - location.lon)
                a = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
                if not math.isfinite(a) or 6371 * 2 * math.asin(math.sqrt(min(1, max(0, a)))) > 200:
                    return None
        except (ValueError, TypeError):
            return None
    if not live_status or not location_fresh or (now - updated).total_seconds() > 900:
        # The current ADS-B aircraft can corroborate a scheduled passenger leg
        # even when the schedule provider has not updated its live data.
        # Only expose the original schedule, never that stale status/position.
        if not resolver_identity and (not aircraft.modeS or not position.callsign or normalize(getattr(flight, "callSign", None)) != normalize(position.callsign)):
            return None
        if set(position.aircraft_categories or ()) & {"military", "cargo", "helicopters", "interesting"} or getattr(flight, "isCargo", None) is not False:
            return None
        def scheduled_utc(leg):
            stamp = getattr(getattr(leg, "scheduledTime", None), "utc", None)
            value = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None
            return value if value and value.tzinfo else None
        try:
            departure = scheduled_utc(flight.departure)
            arrival = scheduled_utc(flight.arrival)
            if not departure or not arrival or not departure < arrival or not departure - timedelta(minutes=30) <= now <= arrival + timedelta(hours=2):
                return None
        except (ValueError, TypeError):
            return None
    def endpoint(data):
        airport = data.airport
        clean = lambda value: value.strip()[:120] if isinstance(value, str) and value.strip() else None
        return AircraftRouteEndpoint(code=clean(airport.iata) or clean(airport.icao), name=clean(airport.name),
                                     city=clean(airport.municipalityName),
                                     scheduled_time_local=clean(getattr(getattr(data, "scheduledTime", None), "local", None)))
    return endpoint(flight.departure), endpoint(flight.arrival)


class ObservedAircraftTrails:
    """Per-worker public, bounded rolling samples. Not a full history archive.

    6,000 aircraft x at most 16 positions, at most six hours. Cold start and
    worker restart mean no earlier observations. No guessed airports/segments.
    """
    aircraft = OrderedDict()

    @classmethod
    def record(cls, positions):
        cutoff = int(time.time()) - 21600
        for key in list(cls.aircraft):
            points = cls.aircraft[key][1]
            while points and points[0].timestamp < cutoff: points.popleft()
            if not points: cls.aircraft.pop(key)
        for position in positions:
            timestamp = position.time_position
            if timestamp is None:
                continue
            key = position.icao24
            existing = cls.aircraft.get(key)
            if existing is None or existing[0] != position.callsign:
                points = deque(maxlen=16)
                cls.aircraft[key] = (position.callsign, points)
            else:
                points = existing[1]
            if points and timestamp <= points[-1].timestamp:
                continue
            # After a coverage gap we cannot establish continuity/flight leg.
            if points and timestamp - points[-1].timestamp > 600:
                points.clear()
            points.append(AircraftTrailPoint(timestamp=timestamp, lat=position.lat, lon=position.lon))
            cls.aircraft.move_to_end(key)
            while len(cls.aircraft) > 6000: cls.aircraft.popitem(last=False)

    @classmethod
    def segments(cls, icao24, callsign, now):
        entry = cls.aircraft.get(icao24)
        if not entry or entry[0] != callsign:
            return []
        rows = [[p.timestamp, p.lat, p.lon, None, None, None, 0] for p in entry[1]]
        return parse_trace(dict(icao=icao24, timestamp=0, trace=rows), icao24, now)


class AircraftContextService:
    cache = OrderedDict()
    pending = {}
    slots = asyncio.Semaphore(4)
    trace_backoff_until = 0.0
    schedule_pool = SchedulePool()
    schedule_task: asyncio.Task | None = None
    next_schedule_check = 0.0

    @staticmethod
    def schedule_key(position, now):
        return json.dumps((position.icao24, position.callsign, now.date().isoformat()), separators=(",", ":"))

    @classmethod
    def verified_schedule(cls, position, now, entries):
        if position.aircraft_categories or not position.callsign:
            return None
        cached = entries.get(cls.schedule_key(position, now))
        if cached and now.timestamp() < cached[0] and cached[1]:
            return AircraftContext.model_validate(cached[1])
        return None

    @classmethod
    async def prioritize_schedules(cls, positions):
        """Read the shared pool; only one worker can warm it per interval."""
        from .service import FlightService
        now = datetime.now(timezone.utc)
        state = await asyncio.to_thread(cls.schedule_pool.snapshot)
        if (positions and now.timestamp() >= state.get("next_batch", 0)
                and time.monotonic() >= cls.next_schedule_check
                and (cls.schedule_task is None or cls.schedule_task.done())):
            cls.next_schedule_check = time.monotonic() + 5
            cls.schedule_task = asyncio.create_task(cls.warm_schedules(positions))
        entries = state.get("entries", {})
        if not positions or not any(expiry > now.timestamp() and value for expiry, value in entries.values()):
            return positions
        verified_keys = {key for key, (expiry, value) in entries.items() if expiry > now.timestamp() and value}
        def confirmed(position):
            return not position.aircraft_categories and position.callsign and cls.schedule_key(position, now) in verified_keys

        result = [p.model_copy(update={"verified_schedule": True}) if confirmed(p) else p for p in positions]
        included = {p.icao24 for p in result}
        # The workers can stride-sample different snapshots. Reserve existing
        # ordinary slots for confirmed aircraft so sampling cannot discard them.
        slots = {}
        for index, p in enumerate(result):
            if not p.aircraft_categories and not p.verified_schedule:
                slots.setdefault(FlightService._continent_key(lat=p.lat, lon=p.lon), []).append(index)
        full = await FlightService._get_cached_global_positions(now=now)
        for p in full:
            if p.icao24 in included or not confirmed(p) or not visible_aircraft(p, int(now.timestamp())):
                continue
            available = slots.get(FlightService._continent_key(lat=p.lat, lon=p.lon))
            if available:
                result[available.pop()] = p.model_copy(update={"verified_schedule": True})
                included.add(p.icao24)
        return result

    @classmethod
    async def warm_schedules(cls, positions):
        from .service import FlightService
        claim = await asyncio.to_thread(cls.schedule_pool.claim)
        if claim:
            lease, cursor, entries = claim
            now = datetime.now(timezone.utc)
            tick = now.timestamp()
            regions = {}
            for position in positions:
                if (not position.callsign or position.aircraft_categories
                        or not FlightService._looks_like_route_callsign(position)
                        or not visible_aircraft(position, int(now.timestamp()))
                        or (position.altitude_feet or 0) < 5000
                        or (position.ground_speed_kt or 0) < 180):
                    continue
                key = cls.schedule_key(position, now)
                if (cached := entries.get(key)) and tick < cached[0]:
                    continue
                region = FlightService._continent_key(lat=position.lat, lon=position.lon)
                if not FlightService._is_preferred_global_airline(position, region):
                    continue
                regions.setdefault(region, []).append(position)
            ordered = sorted(regions)
            if not ordered:
                return
            start = cursor % len(ordered)
            ordered = ordered[start:] + ordered[:start]
            batch = []
            for offset in range(12):
                for region in ordered:
                    if offset < len(regions[region]):
                        batch.append(regions[region][offset])
                    if len(batch) == 12:
                        break
                if len(batch) == 12:
                    break
            await cls.precheck_schedules(batch, lease)

    @classmethod
    async def precheck_schedules(cls, positions, lease):
        async def check(position):
            now = datetime.now(timezone.utc)
            if not visible_aircraft(position, int(now.timestamp())):
                return None
            result = AircraftContext(icao24=position.icao24)
            await cls.route(result, position, now)
            key = cls.schedule_key(position, now)
            complete = result.route_status == "available" and all(
                endpoint and endpoint.code and endpoint.scheduled_time_local
                for endpoint in (result.origin, result.destination)
            )
            expiry = time.time() + (900 if complete else 600)
            if complete and result._schedule_valid_until:
                expiry = min(expiry, result._schedule_valid_until)
            return key, [expiry, result.model_dump(mode="json") if complete else None], result.route_status

        for offset in range(0, len(positions), 3):
            checked = [row for row in await asyncio.gather(*(check(p) for p in positions[offset:offset+3])) if row]
            backoff = bool(checked) and all(status == "provider_unavailable" for _, _, status in checked)
            if checked:
                await asyncio.to_thread(cls.schedule_pool.publish, lease,
                                        {key: entry for key, entry, _ in checked}, backoff)
            if backoff:
                break

    @classmethod
    async def get(cls, icao24: str) -> AircraftContext:
        from .service import FlightService
        now = datetime.now(timezone.utc)
        positions = await FlightService._get_cached_global_positions(now=now)
        from .aircraft_metadata import visible_aircraft
        position = next((p for p in positions if p.icao24 == icao24 and visible_aircraft(p, int(now.timestamp()))), None)
        # Restrict enrichment to real currently available aircraft, not arbitrary
        # client-supplied callsigns/provider paths. No guest/account data in cache.
        if position is None:
            return AircraftContext(icao24=icao24)
        key = (icao24, position.callsign, now.date())
        cached = cls.cache.get(key)
        state = await asyncio.to_thread(cls.schedule_pool.snapshot)
        schedule = cls.verified_schedule(position, now, state.get("entries", {}))
        if cached and time.monotonic() - cached[0] < 60:
            result = cached[1].model_copy(deep=True)
            if schedule:
                result.origin, result.destination = schedule.origin, schedule.destination
                result.route_status = schedule.route_status
            if result.trail_source != "adsbexchange":
                samples = ObservedAircraftTrails.segments(icao24, position.callsign, int(now.timestamp()))
                if samples:
                    result.segments, result.trail_status, result.trail_source = samples, "partial", "sofly_observed"
            return result
        if key not in cls.pending:
            if len(cls.pending) >= 32:
                return AircraftContext(icao24=icao24, trail_status="provider_unavailable", route_status="provider_unavailable")
            async def run():
                try:
                    async with cls.slots:
                        result = AircraftContext(icao24=icao24)
                        if schedule:
                            result.origin, result.destination = schedule.origin, schedule.destination
                            result.route_status = schedule.route_status
                            await cls.trace(result, now)
                        else:
                            await asyncio.gather(cls.trace(result, now), cls.route(result, position, now, full_resolver=True))
                        if not result.segments:
                            result.segments = ObservedAircraftTrails.segments(icao24, position.callsign, int(now.timestamp()))
                            if result.segments:
                                result.trail_status = "partial"
                                result.trail_source = "sofly_observed"
                    cls.cache[key] = (time.monotonic(), result)
                    cls.cache.move_to_end(key)
                    while len(cls.cache) > 256: cls.cache.popitem(last=False)
                    return result
                finally:
                    cls.pending.pop(key, None)
            cls.pending[key] = asyncio.create_task(run())
        return (await asyncio.shield(cls.pending[key])).model_copy(deep=True)

    @classmethod
    async def trace(cls, result, now):
        key = (settings.ADSBEXCHANGE_API_KEY or "").strip()
        if not settings.ADSBEXCHANGE_TRACE_ENABLED:
            result.trail_status = "not_enabled"
            return
        if not key or time.monotonic() < cls.trace_backoff_until:
            result.trail_status = "provider_unavailable"
            return
        try:
            icao = result.icao24
            base = settings.ADSBEXCHANGE_BASE_URL.rstrip("/")
            # Use only the documented trace API with the existing account.
            # Permission/payment failures are not worked around or upgraded.
            async with httpx.AsyncClient(timeout=12) as client:
                headers = {settings.ADSBEXCHANGE_AUTH_HEADER: key, "Accept-Encoding": "gzip"}
                if settings.ADSBEXCHANGE_RAPIDAPI_HOST:
                    headers["x-rapidapi-host"] = settings.ADSBEXCHANGE_RAPIDAPI_HOST
                async with client.stream("GET", f"{base}/traces/{icao[-2:]}/trace_recent_{icao}.json", headers=headers) as response:
                    if response.status_code in {401, 402, 403, 429}:
                        cls.trace_backoff_until = time.monotonic() + 300
                    if response.status_code == 404:
                        return
                    if response.status_code != 200:
                        result.trail_status = "provider_unavailable"
                        return
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 8_000_000:
                            result.trail_status = "provider_unavailable"
                            return
                    result.segments = parse_trace(json.loads(data), icao, int(now.timestamp()))
                    result.trail_status = "available" if result.segments else "unavailable"
                    result.trail_source = "adsbexchange" if result.segments else None
        except (httpx.HTTPError, ValueError, TypeError):
            result.trail_status = "provider_unavailable"

    @classmethod
    async def route(cls, result, position, now, *, full_resolver=False):
        from .service import FlightService
        if not FlightService._looks_like_route_callsign(position):
            return
        raw = (position.callsign or position.display_code).strip().upper()
        # The provider lookup is by scheduled flight number, not ADS-B callsign.
        code = FlightService._normalized_callsign_for_tracking(raw) or raw
        client = AerodataboxClient()
        try:
            async with asyncio.timeout(12):
                matches = {}
                for number in dict.fromkeys((code, raw)):
                    async for date, flights in FlightService._global_resolver_flight_batches(
                        client, number, now, include_next_day=full_resolver
                    ):
                        for flight in flights:
                            endpoints = matched_route(flight, position, now, resolver_number=code if full_resolver else None)
                            if endpoints:
                                key = tuple((e.code or e.name, e.scheduled_time_local) for e in endpoints)
                                arrival_utc = getattr(getattr(flight.arrival, "scheduledTime", None), "utc", None)
                                matches[key] = (endpoints, arrival_utc)
                        if full_resolver and len(matches) == 1:
                            # An already corroborated exact leg should not be
                            # lost to a timeout fetching optional dates.
                            break
                        if matches and date != now.date().isoformat():
                            break
                    if matches:
                        break
                if len(matches) == 1:
                    endpoints, arrival_utc = next(iter(matches.values()))
                    if arrival_utc:
                        try:
                            arrival = datetime.fromisoformat(arrival_utc.replace("Z", "+00:00"))
                            if arrival.tzinfo:
                                result._schedule_valid_until = (arrival + timedelta(minutes=30)).timestamp()
                        except (ValueError, TypeError):
                            pass
                    result.origin, result.destination = endpoints
                    result.route_status = "available"
        except Exception:
            result.route_status = "provider_unavailable"
        finally:
            await client.client.aclose()
