"""Verified commercial globe route fallback and exact schedule enrichment."""

import asyncio
import math
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .schedule_pool import SchedulePool


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def match_route(rows, callsign, address, observed_at, airline):
    return _match_route(rows, callsign, address, observed_at, airline)[0]


def _match_route(rows, callsign, address, observed_at, airline, *, registration=None, nonstandard=False):
    match, reason = _match_leg(rows, callsign, address, observed_at, airline,
                               registration=registration, nonstandard=nonstandard)
    return match[:2] if match else None, reason


def _match_leg(rows, callsign, address, observed_at, airline, *, registration=None, nonstandard=False):
    if not isinstance(rows, list):
        return None, "invalid_response"
    if not rows:
        return None, "no_rows"
    matches = {}
    miss = "identity_mismatch"
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("hex")).lower() != address:
            continue
        if registration:
            if str(row.get("reg")).strip().upper() != registration:
                continue
        elif str(row.get("callsign")).strip().upper() != callsign:
            continue
        if airline:
            carrier_matches = re.fullmatch(re.escape(airline) + r"\d{1,4}[A-Z]?", str(row.get("flight")))
        else:
            operator = row.get("operated_as") or row.get("operating_as")
            carrier_matches = (
                (bool(re.fullmatch(r"[A-Z]{3}", str(operator))) if nonstandard else operator == callsign[:3])
                and re.fullmatch(r"[A-Z0-9]{2}\d{1,4}[A-Z]?", str(row.get("flight")))
            )
        if not carrier_matches:
            if miss == "identity_mismatch":
                miss = "carrier_not_verified"
            continue
        start, end = (_timestamp(row.get(field)) for field in ("first_seen", "last_seen"))
        landed = _timestamp(row.get("datetime_landed"))
        if (start is None or end is None or end < start
                or not start <= observed_at <= min(end, landed if landed is not None else end)):
            miss = "observation_outside_leg"
            continue
        flight_id = row.get("fr24_id")
        if not isinstance(flight_id, str) or not flight_id:
            return None, "missing_flight_id"
        codes = tuple(
            row.get(prefix + "_iata") or row.get(prefix + "_icao") for prefix in ("orig", "dest")
        )
        if (not all(isinstance(code, str) and re.fullmatch(r"[A-Z]{3,4}", code) for code in codes)
                or codes[0] == codes[1]):
            return None, "invalid_airports"
        leg = (*codes, row["flight"], _timestamp(row.get("datetime_takeoff")) or start)
        if flight_id in matches and matches[flight_id] != leg:
            return None, "conflicting_leg"
        matches[flight_id] = leg
    if len(matches) == 1:
        return next(iter(matches.values())), "matched"
    return None, "ambiguous_leg" if matches else miss


_request_budget = SchedulePool(Path(tempfile.gettempdir()) / f"sofly-fr24-budget-v1-{os.getuid()}")
_local_backoff_until = 0.0
_verified_routes = SchedulePool(Path(tempfile.gettempdir()) / f"sofly-fr24-routes-v1-{os.getuid()}")


class RetryReason(str):
    def __new__(cls, reason, delay):
        result = super().__new__(cls, reason)
        result.retry_after = max(1, math.ceil(delay))
        return result


def _reserve_request():
    # All API workers on this host share one budget. A lock or storage failure
    # must not turn into an unmetered provider request.
    reason = "request_budget_unavailable"

    def reserve(state):
        nonlocal reason
        now = time.time()
        recent = [stamp for stamp in state.get("requests", []) if now - stamp < 60]
        burst = [stamp for stamp in recent if now - stamp < 6]
        backoff = state.get("backoff_until", 0) - now
        minute_wait = sorted(recent)[-10] + 60 - now if len(recent) >= 10 else 0
        burst_wait = sorted(burst)[-2] + 6 - now if len(burst) >= 2 else 0
        delay, tag = max((backoff, "provider_backoff"),
                         (minute_wait, "request_minute_limit"), (burst_wait, "request_burst_limit"))
        if delay > 0:
            reason = RetryReason(tag, delay)
        else:
            state["requests"] = [*recent, now]
            return True
        return None

    return None if _request_budget.update(reserve) else reason


def _record_provider_backoff():
    def update(state):
        state["backoff_until"] = max(state.get("backoff_until", 0), time.time() + 60)
        return True

    _request_budget.update(update)


def _cached_route(key):
    value = _verified_routes.snapshot().get(key)
    if value and value[0] > time.time():
        return tuple(value[1])
    return None


def _remember_route(key, route):
    def update(state):
        now = time.time()
        live = [(k, v) for k, v in state.items() if k != key and v[0] > now]
        state.clear()
        state.update(live[-63:])
        state[key] = [now + 15, route]
        return True

    _verified_routes.update(update)


async def lookup_route(position, token, airline, *, include_leg=False):
    global _local_backoff_until
    callsign = (position.callsign or "").strip().upper()
    address = position.icao24.lower()
    observed_at = position.time_position
    if not token:
        return None, "not_configured"
    if position.on_ground or "military" in position.aircraft_categories:
        return None, "ineligible_aircraft"
    nonstandard = not re.fullmatch(r"[A-Z]{3}[0-9][A-Z0-9]{0,4}", callsign)
    registration = None
    if nonstandard:
        candidate = (getattr(position, "registration", None) or "").strip().upper()
        if re.fullmatch(r"[A-Z0-9]{1,5}-?[A-Z0-9]{1,5}", candidate):
            registration = candidate
        if not registration and not re.fullmatch(r"[A-Z0-9]{1,8}", callsign):
            return None, "missing_usable_aircraft_identifier"
        airline = None
    if not re.fullmatch(r"[0-9a-f]{6}", address):
        return None, "invalid_aircraft_address"
    if not observed_at:
        return None, "missing_observation"
    now = datetime.now(timezone.utc)
    if not 0 <= now.timestamp() - observed_at <= 300:
        return None, "observation_not_fresh"
    # Identical ADS-B observations only: never carry a route to a new observation
    # (even on the same airframe and callsign). Read before the provider cooldown.
    key = f"{address}:{callsign}:{observed_at}:{airline or ''}"
    if nonstandard:
        key += f":nonstandard:{registration or ''}"
    route = await asyncio.to_thread(_cached_route, key)
    if route:
        return route if include_leg else route[:2], "matched_repeat"
    if time.monotonic() < _local_backoff_until:
        return None, RetryReason("provider_backoff", _local_backoff_until - time.monotonic())
    budget_reason = await asyncio.to_thread(_reserve_request)
    if budget_reason:
        return None, budget_reason
    observed = datetime.fromtimestamp(observed_at, timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S"
    identity_filter = {"registrations": registration} if registration else {"callsigns": callsign}
    if nonstandard:
        identity_filter["categories"] = "P,C"
    try:
        async with asyncio.timeout(2), httpx.AsyncClient(timeout=1.8) as client:
            response = await client.get(
                "https://fr24api.flightradar24.com/api/flight-summary/light",
                params={**identity_filter, "limit": 20,
                        "flight_datetime_from": (observed - timedelta(hours=20)).strftime(fmt),
                        "flight_datetime_to": now.strftime(fmt)},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                         "Accept-Version": "v1"},
            )
            if response.status_code in (401, 402, 403, 429):
                _local_backoff_until = time.monotonic() + 60
                await asyncio.to_thread(_record_provider_backoff)
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(rows, list) and len(rows) >= 20:
                return None, "result_limit"
            route, reason = _match_leg(rows, callsign, address, observed_at, airline,
                                         registration=registration, nonstandard=nonstandard)
        if route:
            await asyncio.to_thread(_remember_route, key, route)
        return (route if include_leg else route[:2]) if route else None, reason
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        if status == 429:
            return None, RetryReason("provider_rate_limited", _local_backoff_until - time.monotonic())
        return None, {
            401: "provider_unauthorized", 402: "provider_payment_required",
            403: "provider_forbidden", 429: "provider_rate_limited",
        }.get(status, "provider_http_error")
    except (httpx.TimeoutException, TimeoutError):
        return None, "provider_timeout"
    except httpx.HTTPError:
        return None, "provider_transport_error"
    except ValueError:
        return None, "invalid_response"


async def enrich_schedule(position, leg):
    """Look up the verified FR24 flight number using the search provider."""
    from .aircraft_context import matched_route, schedule_window
    from .api_client import AerodataboxClient
    from .service import GlobalLiveFlightResolveMatch, NormalizedGlobalFlightResolveCandidate

    origin, destination, number, leg_start = leg
    observed = datetime.fromtimestamp(position.time_position, timezone.utc)
    day = datetime.fromtimestamp(leg_start, timezone.utc).date()
    dates = [(day + timedelta(days=offset)).isoformat() for offset in (0, -1, 1)]
    client = AerodataboxClient()
    try:
        batches = await asyncio.gather(*(client.get_flight(number, day, with_location=True)
                                         for day in dates), return_exceptions=True)
    finally:
        await client.client.aclose()

    # A failed date lookup leaves ambiguity unresolved. Keep the FR24 route
    # alone rather than trusting a partial view of repeated flight numbers.
    if any(isinstance(rows, BaseException) for rows in batches):
        return None
    matches = {}
    for date, rows in zip(dates, batches):
        for flight in rows:
            if (flight.number.replace(" ", "").upper() != number or flight.isCargo is not False
                    or not flight.aircraft or (flight.aircraft.modeS or "").lower() != position.icao24
                    or not schedule_window(flight, position, observed, arrival_grace=timedelta(minutes=30))):
                continue
            airports = [getattr(movement, "airport", None) for movement in (flight.departure, flight.arrival)]
            if not all(airport and code in (airport.iata, airport.icao)
                       for airport, code in zip(airports, (origin, destination))):
                continue
            endpoints = matched_route(flight, position, datetime.now(timezone.utc), resolver_number=number)
            if not endpoints or not endpoints[0].scheduled_time_local:
                continue
            departure = _timestamp(flight.departure.scheduledTime.utc)
            if not departure or abs(departure - leg_start) > 6 * 3600:
                continue
            arrival = _timestamp(flight.arrival.scheduledTime.utc) if flight.arrival.scheduledTime else None
            matches.setdefault((departure, arrival), (endpoints, GlobalLiveFlightResolveMatch(
                candidate=NormalizedGlobalFlightResolveCandidate(number, position.icao24),
                departure_date=date, live_flights=[flight], live_flight=flight,
            )))
    return next(iter(matches.values())) if len(matches) == 1 else None
