"""Display-only commercial globe route fallback; no tracking or schedule writes."""

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

import httpx


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def match_route(rows, callsign, address, observed_at, airline):
    if not isinstance(rows, list):
        return None
    matches = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (str(row.get("callsign")).strip().upper() != callsign
                or str(row.get("hex")).lower() != address
                or not re.fullmatch(re.escape(airline) + r"\d{1,4}[A-Z]?", str(row.get("flight")))):
            continue
        start, end = (_timestamp(row.get(field)) for field in ("first_seen", "last_seen"))
        landed = _timestamp(row.get("datetime_landed"))
        if (start is None or end is None or end < start
                or not start <= observed_at <= min(end, landed if landed is not None else end)):
            continue
        flight_id = row.get("fr24_id")
        if not isinstance(flight_id, str) or not flight_id:
            return None
        codes = tuple(
            row.get(prefix + "_iata") or row.get(prefix + "_icao") for prefix in ("orig", "dest")
        )
        if (not all(isinstance(code, str) and re.fullmatch(r"[A-Z]{3,4}", code) for code in codes)
                or codes[0] == codes[1]):
            return None
        if flight_id in matches and matches[flight_id] != codes:
            return None
        matches[flight_id] = codes
    return next(iter(matches.values())) if len(matches) == 1 else None


# Bound provider use within each worker. Also rely on the vendor's account cap.
_next_request = 0.0


async def lookup_route(position, token, airline):
    global _next_request
    callsign = (position.callsign or "").strip().upper()
    address = position.icao24.lower()
    observed_at = position.time_position
    if (not token or not airline or not observed_at or position.on_ground
            or "military" in position.aircraft_categories
            or not re.fullmatch(r"[A-Z]{3}[0-9][A-Z0-9]{0,4}", callsign)
            or not re.fullmatch(r"[0-9a-f]{6}", address)):
        return None
    now = datetime.now(timezone.utc)
    if not 0 <= now.timestamp() - observed_at <= 300 or time.monotonic() < _next_request:
        return None
    _next_request = time.monotonic() + 6
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
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or len(rows) >= 20:
                return None
            return match_route(rows, callsign, address, observed_at, airline)
    except (httpx.HTTPError, ValueError, TimeoutError):
        return None
