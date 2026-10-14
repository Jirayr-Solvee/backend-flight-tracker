"""Public flight-share links: tokens, expiry and the sanitized live state.

Everything here reads database rows that webhooks and the fetcher already keep
fresh. Nothing in this module may call a flight-data provider, and nothing may
log a token, its hash, a share URL, a user id or a flight id.
"""

import base64
import hashlib
import hmac
import math
import re
from datetime import date, datetime, time as day_time, timedelta, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, update

from ..config import settings
from ..models.aerodatabox import FlightStatusEnum
from ..models.flight import Airport, Flight, FlightOriginAndDestinationInformation
from ..models.flight_share import FlightShare, current_time_ms
from ..models.user import UserFlightLink

SCHEMA_VERSION = 1
REFRESH_AFTER_SECONDS = 30
TOKEN_LENGTH = 22
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{22}")
REVOKE_REASONS = frozenset(("owner", "flight_removed", "account_deleted"))

ARRIVAL_GRACE = timedelta(hours=6)
DEPARTURE_ONLY_GRACE = timedelta(hours=24)
SERVICE_DATE_GRACE = timedelta(days=2)
DELAY_THRESHOLD_MINUTES = 15

STATUS_VALUES = frozenset(status.value for status in FlightStatusEnum)
AIRBORNE_STATUSES = frozenset(("Departed", "EnRoute", "Approaching"))
CANCELED_STATUSES = frozenset(("Canceled", "CanceledUncertain"))
PRE_DEPARTURE_STATUSES = frozenset(("Expected", "CheckIn", "Boarding", "GateClosed", "Delayed"))

# Provider placeholders the app already treats as missing operational text.
_MISSING_TEXT = frozenset(("", "--", "—", "unknown", "none", "null", "n/a"))
_MAX_TEXT_LENGTH = 200
# Provider years far outside this range would overflow offset and expiry
# arithmetic; such a value is unusable data, not a server error.
_MIN_YEAR, _MAX_YEAR = 1971, 2200
# Owner of rows left behind by a deleted account. No authenticated user can
# have an empty id, and the row no longer links the account to a flight.
DELETED_ACCOUNT_OWNER = ""
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Field prefix on Departure/Arrival for each public time kind.
_TIME_FIELDS = (
    ("scheduled", "scheduled"), ("revised", "revised"),
    ("predicted", "predicted"), ("actual", "runway"),
)
_BEST_ORDER = ("actual", "predicted", "revised", "scheduled")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------- Token ------------------


def _token_key() -> bytes:
    return hmac.new(
        settings.JWT_SECRET.encode("utf-8"), b"sofly-flight-share:v1", hashlib.sha256
    ).digest()


def share_token(share_id: str) -> str:
    digest = hmac.new(_token_key(), share_id.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:TOKEN_LENGTH]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def is_valid_token(token: str) -> bool:
    return isinstance(token, str) and TOKEN_PATTERN.fullmatch(token) is not None


def share_url(share: FlightShare) -> str:
    return settings.FLIGHT_SHARE_BASE_URL + share_token(share.id)


# ---------------- Times and expiry ------------------


def _parse_time(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 64:
        return None
    # Provider form "2026-10-14 08:30Z" / "2026-10-14 10:30+02:00".
    text = text.replace(" ", "T", 1)
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if _MIN_YEAR <= parsed.year <= _MAX_YEAR else None


def _with_inferred_offset(local: datetime, utc: datetime | None) -> datetime | None:
    # Airports store no time zone. A naive local value only gains an offset when
    # its UTC twin proves one; otherwise it cannot be shown as airport-local.
    if utc is None:
        return None
    minutes = round((local - utc.replace(tzinfo=None)).total_seconds() / 900) * 15
    if abs(minutes) > 14 * 60:
        return None
    return local.replace(tzinfo=timezone(timedelta(minutes=minutes)))


def _time_pair(utc_value: str | None, local_value: str | None) -> tuple[datetime, datetime | None] | None:
    utc = _parse_time(utc_value)
    if utc is not None:
        utc = utc.replace(tzinfo=timezone.utc) if utc.tzinfo is None else utc.astimezone(timezone.utc)
    local = _parse_time(local_value)
    if local is not None and local.tzinfo is None:
        local = _with_inferred_offset(local, utc)
    if utc is None and local is not None:
        utc = local.astimezone(timezone.utc)
    if utc is None:
        return None
    return utc.replace(microsecond=0), local.replace(microsecond=0) if local else None


def _leg_times(
    detail: FlightOriginAndDestinationInformation | None,
) -> dict[str, tuple[datetime, datetime | None]]:
    times = {}
    if detail is None:
        return times
    for kind, prefix in _TIME_FIELDS:
        pair = _time_pair(
            getattr(detail, f"{prefix}_time_utc", None),
            getattr(detail, f"{prefix}_time_local", None),
        )
        if pair is not None:
            times[kind] = pair
    return times


def _best(times: dict[str, tuple[datetime, datetime | None]]) -> tuple[str, datetime, datetime | None] | None:
    for kind in _BEST_ORDER:
        if kind in times:
            return (kind, *times[kind])
    return None


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(_UTC_FORMAT)


def share_expires_at(flight: Flight) -> datetime:
    # Recomputed on every read: delays move the end of a share with the flight.
    arrival = _best(_leg_times(flight.arrival))
    if arrival is not None:
        return arrival[1] + ARRIVAL_GRACE
    departure = _best(_leg_times(flight.departure))
    if departure is not None:
        return departure[1] + DEPARTURE_ONLY_GRACE
    try:
        service_date = date.fromisoformat((flight.date or "").strip()[:10])
    except ValueError:
        service_date = None
    if service_date is None or not _MIN_YEAR <= service_date.year <= _MAX_YEAR:
        # Without any usable time the link cannot prove it is still relevant.
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return datetime.combine(service_date, day_time.min, tzinfo=timezone.utc) + SERVICE_DATE_GRACE


# ---------------- Sanitized live state ------------------


def _text(value) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if cleaned.casefold() in _MISSING_TEXT:
        return None
    return cleaned[:_MAX_TEXT_LENGTH]


def _finite(value, lower: float, upper: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not lower <= number <= upper:
        return None
    return number


def status_value(flight: Flight) -> str:
    raw = flight.status.value if hasattr(flight.status, "value") else str(flight.status)
    return raw if raw in STATUS_VALUES else "Unknown"


def _airport(airport: Airport | None) -> dict:
    if airport is None:
        return {"iata": None, "name": None, "city": None, "country_code": None, "lat": None, "lon": None}
    # The airport-search path creates "unknown" rows at 0,0, and the mapper
    # writes 0,0 when the provider omits a location. Neither is a real place.
    placeholder = isinstance(airport.name, str) and airport.name.strip().casefold() == "unknown"
    lat = _finite(airport.lat, -90.0, 90.0)
    lon = _finite(airport.lon, -180.0, 180.0)
    if placeholder or lat is None or lon is None or (lat == 0.0 and lon == 0.0):
        lat = lon = None
    return {
        "iata": _text(airport.iata),
        "name": None if placeholder else _text(airport.name),
        "city": _text(airport.municipality_name),
        "country_code": _text(airport.country_code),
        "lat": lat,
        "lon": lon,
    }


def _time_object(pair: tuple[datetime, datetime | None] | None) -> dict | None:
    if pair is None:
        return None
    utc, local = pair
    return {"utc": utc_text(utc), "local": local.isoformat(timespec="seconds") if local else None}


def _leg(detail: FlightOriginAndDestinationInformation | None) -> tuple[dict, datetime | None, int | None]:
    times = _leg_times(detail)
    best = _best(times)
    scheduled = times.get("scheduled")
    delay_minutes = None
    if best is not None and scheduled is not None:
        delay_minutes = round((best[1] - scheduled[0]).total_seconds() / 60)
    leg = {
        "airport": _airport(detail.airport if detail is not None else None),
        "terminal": _text(detail.terminal) if detail is not None else None,
        "gate": _text(detail.gate) if detail is not None else None,
        "checkin_desk": _text(detail.checkin_desk) if detail is not None else None,
        "baggage_belt": _text(detail.baggage_belt) if detail is not None else None,
        "scheduled": _time_object(times.get("scheduled")),
        "revised": _time_object(times.get("revised")),
        "predicted": _time_object(times.get("predicted")),
        "actual": _time_object(times.get("actual")),
        "best": (
            {"kind": best[0], **_time_object((best[1], best[2]))} if best is not None else None
        ),
        "delay_minutes": delay_minutes,
    }
    return leg, best[1] if best is not None else None, delay_minutes


def flight_phase(status: str, departure: datetime | None, arrival: datetime | None, now: datetime) -> str:
    # Canceled and diverted are terminal facts; their past times must not make
    # a viewer believe the flight flew or landed as planned.
    if status in CANCELED_STATUSES:
        return "canceled"
    if status == "Diverted":
        return "diverted"
    if status == "Arrived" or (arrival is not None and arrival <= now):
        return "landed"
    if status in AIRBORNE_STATUSES or (departure is not None and departure <= now):
        return "airborne"
    if departure is not None or arrival is not None or status in PRE_DEPARTURE_STATUSES:
        return "pre_departure"
    return "unknown"


def flight_progress(phase: str, departure: datetime | None, arrival: datetime | None, now: datetime) -> float | None:
    if phase == "landed":
        return 1.0
    if phase != "airborne" or departure is None or arrival is None or arrival <= departure:
        return None
    elapsed = (now - departure).total_seconds() / (arrival - departure).total_seconds()
    return round(min(max(elapsed, 0.0), 1.0), 4)


def great_circle_point(
    origin: tuple[float, float], destination: tuple[float, float], fraction: float,
) -> tuple[float, float, float] | None:
    """Spherical interpolation plus the initial bearing onward from that point."""
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, destination)
    distance = 2 * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))
    # Identical or antipodal airports have no single great-circle route.
    if distance < 1e-9 or math.sin(distance) < 1e-9:
        return None
    a = math.sin((1 - fraction) * distance) / math.sin(distance)
    b = math.sin(fraction * distance) / math.sin(distance)
    x = a * math.cos(lat1) * math.cos(lon1) + b * math.cos(lat2) * math.cos(lon2)
    y = a * math.cos(lat1) * math.sin(lon1) + b * math.cos(lat2) * math.sin(lon2)
    z = a * math.sin(lat1) + b * math.sin(lat2)
    lat = math.atan2(z, math.sqrt(x * x + y * y))
    lon = math.atan2(y, x)
    if fraction >= 1.0:
        # At the destination, continue the arrival course (reverse bearing + 180).
        heading = (_bearing(lat2, lon2, lat1, lon1) + 180.0) % 360.0
    else:
        heading = _bearing(lat, lon, lat2, lon2)
    return math.degrees(lat), math.degrees(lon), heading


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    delta = lon2 - lon1
    angle = math.atan2(
        math.sin(delta) * math.cos(lat2),
        math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta),
    )
    return (math.degrees(angle) + 360.0) % 360.0


def _position(phase: str, progress: float | None, departure: dict, arrival: dict) -> dict | None:
    if phase != "airborne" or progress is None:
        return None
    if None in (departure["lat"], departure["lon"], arrival["lat"], arrival["lon"]):
        return None
    point = great_circle_point(
        (departure["lat"], departure["lon"]), (arrival["lat"], arrival["lon"]), progress,
    )
    if point is None:
        return None
    lat, lon, heading = point
    return {
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "heading_deg": round(heading, 1),
        # v1 never claims a measured position; ADS-B can add another kind later.
        "kind": "estimated",
    }


def build_live_state(flight: Flight, *, now: datetime, expires_at: datetime) -> dict:
    """The live.json contract. Allowlisted fields only: never ids or user data."""
    status = status_value(flight)
    departure, departure_best, departure_delay = _leg(flight.departure)
    arrival, arrival_best, arrival_delay = _leg(flight.arrival)
    phase = flight_phase(status, departure_best, arrival_best, now)
    progress = flight_progress(phase, departure_best, arrival_best, now)
    # Departure delay matters until take-off; afterwards the arrival estimate does.
    relevant_delay = (
        arrival_delay if phase in ("airborne", "landed") and arrival_delay is not None else departure_delay
    )
    airline = flight.airline
    distance = _finite(flight.distance_km, 0.0, 50_000.0)
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "active",
        "server_time_utc": utc_text(now),
        "expires_at_utc": utc_text(expires_at),
        "refresh_after_seconds": REFRESH_AFTER_SECONDS,
        "phase": phase,
        "progress": progress,
        "position": _position(phase, progress, departure["airport"], arrival["airport"]),
        "flight": {
            "number": _text(flight.number),
            "date": _text(flight.date),
            "status": status,
            "is_delayed": status == "Delayed" or (
                relevant_delay is not None and relevant_delay >= DELAY_THRESHOLD_MINUTES
            ),
            "airline": {
                "name": _text(airline.name) if airline else None,
                "iata": _text(airline.iata) if airline else None,
                "icao": _text(airline.icao) if airline else None,
            },
            "aircraft": {
                "model": _text(flight.aircraft_model),
                "registration": _text(flight.aircraft_reg),
            },
            "distance_km": round(distance) if distance is not None else None,
            "departure": departure,
            "arrival": arrival,
        },
    }


# ---------------- Persistence ------------------


def owns_flight(session: Session, user_id: str, flight_id: int) -> bool:
    return session.exec(
        select(UserFlightLink).where(
            UserFlightLink.user_id == user_id,
            UserFlightLink.flight_id == flight_id,
        )
    ).first() is not None


def find_active_share(session: Session, user_id: str, flight_id: int) -> FlightShare | None:
    return session.exec(
        select(FlightShare).where(
            FlightShare.user_id == user_id,
            FlightShare.flight_id == flight_id,
            FlightShare.revoked_at_ms.is_(None),  # type: ignore[union-attr]
        )
    ).first()


def rekey_if_needed(session: Session, share: FlightShare) -> FlightShare:
    # The token is recomputed from the id. If JWT_SECRET was rotated, the
    # stored hash no longer matches the URL the owner would receive.
    expected = token_hash(share_token(share.id))
    if hmac.compare_digest(share.token_hash, expected):
        return share
    share.token_hash = expected
    session.add(share)
    session.commit()
    session.refresh(share)
    return share


def create_or_reuse_share(session: Session, user_id: str, flight_id: int) -> tuple[FlightShare, bool]:
    existing = find_active_share(session, user_id, flight_id)
    if existing is not None:
        return rekey_if_needed(session, existing), False
    share_id = uuid4().hex
    share = FlightShare(
        id=share_id,
        token_hash=token_hash(share_token(share_id)),
        user_id=user_id,
        flight_id=flight_id,
    )
    session.add(share)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent create won the partial unique index. Return its link.
        session.rollback()
        existing = find_active_share(session, user_id, flight_id)
        if existing is None:
            raise
        return rekey_if_needed(session, existing), False
    session.refresh(share)
    return share, True


def revoke_flight_shares(session: Session, user_id: str, flight_id: int, reason: str) -> bool:
    """Revoke the owner's active link for one flight. The caller commits."""
    if reason not in REVOKE_REASONS:
        raise ValueError("Unsupported revoke reason")
    result = session.exec(
        update(FlightShare)
        .where(
            FlightShare.user_id == user_id,
            FlightShare.flight_id == flight_id,
            FlightShare.revoked_at_ms.is_(None),  # type: ignore[union-attr]
        )
        .values(revoked_at_ms=current_time_ms(), revoke_reason=reason)
    )
    return bool(result.rowcount)


def revoke_user_shares(session: Session, user_id: str, reason: str) -> int:
    """Revoke every active link the account created. The caller commits."""
    if reason not in REVOKE_REASONS:
        raise ValueError("Unsupported revoke reason")
    result = session.exec(
        update(FlightShare)
        .where(
            FlightShare.user_id == user_id,
            FlightShare.revoked_at_ms.is_(None),  # type: ignore[union-attr]
        )
        .values(revoked_at_ms=current_time_ms(), revoke_reason=reason)
    )
    if reason == "account_deleted":
        # Account deletion clears the user's flight links; the revoked rows
        # (earlier ones included) must not keep that user-to-flight history.
        session.exec(
            update(FlightShare)
            .where(FlightShare.user_id == user_id)
            .values(user_id=DELETED_ACCOUNT_OWNER)
        )
    return int(result.rowcount or 0)


def resolve_public_share(
    session: Session, token: str, now: datetime,
) -> tuple[str, FlightShare | None, Flight | None, datetime | None]:
    """Return (state, share, flight, expires_at); state is active|expired|not_found."""
    if not is_valid_token(token):
        return "not_found", None, None, None
    share = session.exec(
        select(FlightShare).where(FlightShare.token_hash == token_hash(token))
    ).first()
    if share is None or share.revoked_at_ms is not None:
        return "not_found", None, None, None
    flight = session.get(Flight, share.flight_id)
    # Revocation hooks cover deletion; the link check also covers rows removed
    # by any other path, so an untracked flight is never published.
    if flight is None or not owns_flight(session, share.user_id, share.flight_id):
        return "not_found", None, None, None
    expires_at = share_expires_at(flight)
    if expires_at <= now:
        return "expired", share, None, expires_at
    return "active", share, flight, expires_at


def record_view(session: Session, share_id: str, now_ms: int) -> None:
    session.exec(
        update(FlightShare)
        .where(FlightShare.id == share_id)
        .values(view_count=FlightShare.view_count + 1, last_viewed_at_ms=now_ms)
    )
    session.commit()
