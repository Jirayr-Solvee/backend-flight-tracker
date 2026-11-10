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
from sqlmodel import SQLModel, Field

from ...config import settings
from .api_client import ADSBExchangeClient, AerodataboxClient
from .aircraft_metadata import visible_aircraft
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


def matched_route(flight, position, now: datetime):
    aircraft = flight.aircraft
    if not aircraft:
        return None
    normalize = lambda value: re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    if aircraft.modeS:
        identity_matches = aircraft.modeS.strip().lower() == position.icao24.strip().lower()
    else:
        identity_matches = bool(position.registration) and normalize(aircraft.reg) == normalize(position.registration)
    if not identity_matches or str(flight.status.value) not in {"EnRoute", "Departed", "Approaching", "Delayed"}:
        return None
    try:
        updated = datetime.fromisoformat(flight.lastUpdatedUtc.replace("Z", "+00:00"))
        if updated.tzinfo is None or not -30 <= (now - updated).total_seconds() <= 900:
            return None
    except (ValueError, TypeError):
        return None
    location = flight.location
    if location is None:
        # Some scheduled passenger legs have no provider position. Require
        # matching live callsign and Mode S, not just a flight number or tail.
        if not aircraft.modeS or not position.callsign or normalize(getattr(flight, "callSign", None)) != normalize(position.callsign):
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
    else:
        if location.lat is None or location.lon is None or not location.reportedAtUtc:
            return None
        if not -90 <= location.lat <= 90 or not -180 <= location.lon <= 180:
            return None
        try:
            reported = datetime.fromisoformat(location.reportedAtUtc.replace("Z", "+00:00"))
            if reported.tzinfo is None or not -30 <= (now - reported).total_seconds() <= 900:
                return None
            lat1, lat2 = math.radians(position.lat), math.radians(location.lat)
            dlon = math.radians(position.lon - location.lon)
            a = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
            if not math.isfinite(a) or 6371 * 2 * math.asin(math.sqrt(min(1, max(0, a)))) > 200:
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
        if cached and time.monotonic() - cached[0] < 60:
            result = cached[1].model_copy(deep=True)
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
                        await asyncio.gather(cls.trace(result, now), cls.route(result, position, now))
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
    async def route(cls, result, position, now):
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
                dates = (now.date(), now.date() - timedelta(days=1))
                for number in dict.fromkeys((code, raw)):
                    for date in dates:
                        flights = await client.get_flight(number, date.isoformat(), with_location=True)
                        for flight in flights:
                            endpoints = matched_route(flight, position, now)
                            if endpoints:
                                key = tuple((e.code, e.name) for e in endpoints)
                                matches[key] = endpoints
                    if matches:
                        break
                if len(matches) == 1:
                    result.origin, result.destination = next(iter(matches.values()))
                    result.route_status = "available"
        except Exception:
            result.route_status = "provider_unavailable"
        finally:
            await client.client.aclose()
