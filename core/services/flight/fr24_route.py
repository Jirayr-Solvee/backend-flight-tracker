"""Display-only commercial globe route fallback; no tracking or schedule writes."""

import asyncio
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


def _match_route(rows, callsign, address, observed_at, airline):
    if not isinstance(rows, list):
        return None, "invalid_response"
    if not rows:
        return None, "no_rows"
    matches = {}
    miss = "identity_mismatch"
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (str(row.get("callsign")).strip().upper() != callsign
                or str(row.get("hex")).lower() != address):
            continue
        if airline:
            carrier_matches = re.fullmatch(re.escape(airline) + r"\d{1,4}[A-Z]?", str(row.get("flight")))
        else:
            operator = row.get("operated_as") or row.get("operating_as")
            carrier_matches = (
                operator == callsign[:3]
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
        if flight_id in matches and matches[flight_id] != codes:
            return None, "conflicting_airports"
        matches[flight_id] = codes
    if len(matches) == 1:
        return next(iter(matches.values())), "matched"
    return None, "ambiguous_leg" if matches else miss


# Bound provider use within each worker. Also rely on the vendor's account cap.
_next_request = 0.0
_cooldown_reason = "request_cooldown"
_verified_routes = SchedulePool(Path(tempfile.gettempdir()) / f"sofly-fr24-routes-v1-{os.getuid()}")


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


async def lookup_route(position, token, airline):
    global _next_request, _cooldown_reason
    callsign = (position.callsign or "").strip().upper()
    address = position.icao24.lower()
    observed_at = position.time_position
    if not token:
        return None, "not_configured"
    if position.on_ground or "military" in position.aircraft_categories:
        return None, "ineligible_aircraft"
    if not re.fullmatch(r"[A-Z]{3}[0-9][A-Z0-9]{0,4}", callsign):
        return None, "unsupported_callsign"
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
    route = await asyncio.to_thread(_cached_route, key)
    if route:
        return route, "matched_repeat"
    if time.monotonic() < _next_request:
        return None, _cooldown_reason
    _next_request = time.monotonic() + 6
    _cooldown_reason = "request_cooldown"
    observed = datetime.fromtimestamp(observed_at, timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S"
    try:
        async with asyncio.timeout(2), httpx.AsyncClient(timeout=1.8) as client:
            response = await client.get(
                "https://fr24api.flightradar24.com/api/flight-summary/light",
                params={"callsigns": callsign, "limit": 20,
                        "flight_datetime_from": (observed - timedelta(hours=20)).strftime(fmt),
                        "flight_datetime_to": now.strftime(fmt)},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                         "Accept-Version": "v1"},
            )
            if response.status_code in (401, 402, 403, 429):
                _next_request = time.monotonic() + 60
                _cooldown_reason = "provider_backoff"
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(rows, list) and len(rows) >= 20:
                return None, "result_limit"
            route, reason = _match_route(rows, callsign, address, observed_at, airline)
        if route:
            await asyncio.to_thread(_remember_route, key, route)
        return route, reason
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
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
