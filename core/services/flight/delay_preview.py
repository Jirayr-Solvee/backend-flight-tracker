"""Onboarding delay preview: one real prediction per account and installation.

`POST /flights/delay-preview` serves the closable_flow_2026_10 test arm's plain
flight preview. It needs neither a subscription nor a tracked flight, and it
never creates a Flight, UserFlightLink, premium entitlement or webhook.

- Only an account holding a server-randomized `closable_own_flight` assignment
  for the installation it sends may call it (control parity, server side).
- The first request binds the account and the installation to one flight
  identity, for good. The same flight later returns the stored result without
  a provider call; any other flight gets 429. Only a transient provider or
  capacity failure allows a retry, for the same flight, with at most two
  uncached builds per claim.
- Composed results (60 min), airport delay (30 min) and flight-number history
  (12 h) are cached in the application database, so all API workers share
  them. Uncached builds reserve a global daily and per-minute budget.
- A failed provider call is never scored as "low risk", and too few signals
  return `insufficient_data` without a number.
- Log lines carry bounded labels only: never account, installation or flight
  identifiers, flight numbers, airports, times or tokens.
"""

import asyncio
import contextvars
import hashlib
import json
import logging
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, delete, or_, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, select

from ... import closable_flow_policy as closable_flow
from ...activation_journey_contract import CLOSABLE_FLOW_ID, ActivationJourneyContext
from ...config import settings
from ...models.activation_journey import ActivationJourneyAssignment
from ...models.aerodatabox import FlightStatusEnum
from ...models.delay_preview import DelayPreviewBudget, DelayPreviewCache, DelayPreviewClaim
from ...models.experiment import current_time_ms
from ...models.flight import Flight
from ..activation_journey import identity_key
from .delay_risk import DelayRiskResponse, DelayRiskService

RESULT_TTL_MS = 60 * 60 * 1000
AIRPORT_TTL_MS = 30 * 60 * 1000
HISTORY_TTL_MS = 12 * 60 * 60 * 1000
LEASE_MS = 15_000
RETRY_COOLDOWN_MS = 60_000
MAX_BUILDS = 2
PROVIDER_DEADLINE_SECONDS = 8.0
PRUNE_LIMIT = 100
BUDGET_RETENTION_DAYS = 2
DATE_WINDOW_PAST_DAYS = 2
DATE_WINDOW_FUTURE_DAYS = 366

# Statuses before the aircraft leaves the gate. The departure runway time must
# also be empty.
PRE_DEPARTURE_STATUSES = frozenset(("Expected", "Unknown", "CheckIn", "Boarding", "GateClosed", "Delayed"))
FINAL_STATES = frozenset(("ready", "insufficient_data"))

_NUMBER = re.compile(r"[A-Z0-9]{2,3}[0-9]{1,4}[A-Z]?")
_IATA = re.compile(r"[A-Z]{3}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_UTC_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}(:[0-9]{2})?Z$"


# ---------------------------------------------------------------- identity


def _valid_calendar_date(value: str) -> bool:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def flight_identity(number: Any, date_value: Any, departure: Any, arrival: Any) -> str | None:
    """Mirror of the app's FlightFunnelIdentityPolicy.make (FunnelIdentityPolicy.swift).

    sha256("NUMBER|YYYY-MM-DD|DEP|ARR"): number without spaces, uppercased,
    2-3 airline characters plus 1-4 digits and an optional suffix, containing
    a letter; three-letter airports; a real calendar date. None when invalid.
    """
    if not all(isinstance(value, str) for value in (number, date_value, departure, arrival)):
        return None
    normalized = number.strip().replace(" ", "").upper()
    departure_code = departure.strip().upper()
    arrival_code = arrival.strip().upper()
    if (
        not _NUMBER.fullmatch(normalized)
        or not re.search(r"[A-Z]", normalized)
        or not _IATA.fullmatch(departure_code)
        or not _IATA.fullmatch(arrival_code)
        or not _valid_calendar_date(date_value)
    ):
        return None
    joined = "|".join((normalized, date_value, departure_code, arrival_code))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _normalized_number(number: str) -> str:
    return number.strip().replace(" ", "").upper()


# ---------------------------------------------------------------- request


UtcTime = Annotated[str, Field(max_length=25, pattern=_UTC_PATTERN)]


class DelayPreviewTimes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheduled_utc: UtcTime | None = None
    revised_utc: UtcTime | None = None
    predicted_utc: UtcTime | None = None
    runway_utc: UtcTime | None = None


class DelayPreviewFlight(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: str = Field(min_length=1, max_length=12)
    date: str = Field(min_length=10, max_length=10, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    departure_iata: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    arrival_iata: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    status: FlightStatusEnum
    departure: DelayPreviewTimes = Field(default_factory=DelayPreviewTimes)
    arrival: DelayPreviewTimes = Field(default_factory=DelayPreviewTimes)


class DelayPreviewRequest(BaseModel):
    """Body of POST /flights/delay-preview. Nothing travels in the URL."""
    model_config = ConfigDict(extra="forbid")
    # The closable-flow installation (ClosableFlowExperiment.current.installationID).
    installation_id: UUID
    flight_identity: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    # Only for exact results that already have a backend Flight row.
    flight_id: Annotated[int, Field(strict=True, ge=1, le=9_223_372_036_854_775_807)] | None = None
    flight: DelayPreviewFlight


class DelayPreviewEnvelope(BaseModel):
    """Documentation model for 200/202 bodies. `prediction` is DelayRiskResponse."""
    state: Literal["ready", "insufficient_data", "pending"]
    cache: Literal["computed", "flight_cache", "stored", "none"]
    flight_identity: str
    primary_reason: str | None = None
    prediction: DelayRiskResponse | None = None
    retry_after_ms: int | None = None


# ---------------------------------------------------------------- outcome


@dataclass
class PreviewOutcome:
    status_code: int
    body: dict
    outcome: str  # bounded log label
    cache: str = "none"
    provider_calls: int = 0
    headers: dict[str, str] = field(default_factory=dict)


def _error(
    status_code: int,
    code: str,
    *,
    outcome: str,
    retryable: bool = False,
    retry_after: int | None = None,
    provider_calls: int = 0,
) -> PreviewOutcome:
    headers = {"Retry-After": str(max(1, int(retry_after)))} if retry_after is not None else {}
    return PreviewOutcome(
        status_code=status_code,
        body={"detail": {"code": code, "retryable": retryable}},
        outcome=outcome,
        provider_calls=provider_calls,
        headers=headers,
    )


def _respond(envelope: dict, cache: str, *, outcome: str | None = None, provider_calls: int = 0) -> PreviewOutcome:
    body = {
        "state": envelope["state"],
        "cache": cache,
        "flight_identity": envelope["flight_identity"],
        "primary_reason": envelope.get("primary_reason"),
        "prediction": envelope.get("prediction"),
    }
    return PreviewOutcome(
        status_code=200,
        body=body,
        outcome=outcome or envelope["state"],
        cache=cache,
        provider_calls=provider_calls,
    )


def _pending(identity: str) -> PreviewOutcome:
    return PreviewOutcome(
        status_code=202,
        body={
            "state": "pending",
            "cache": "none",
            "flight_identity": identity,
            "primary_reason": None,
            "prediction": None,
            "retry_after_ms": 1000,
        },
        outcome="pending",
    )


def _invalid() -> PreviewOutcome:
    return _error(422, "delay_preview_invalid_flight", outcome="invalid")


def _unavailable(*, retryable: bool, retry_after: int | None = None, provider_calls: int = 0) -> PreviewOutcome:
    return _error(
        503,
        "delay_preview_unavailable",
        outcome="unavailable",
        retryable=retryable,
        retry_after=retry_after if retryable else None,
        provider_calls=provider_calls,
    )


# ---------------------------------------------------------------- inputs


@dataclass(frozen=True)
class _Airport:
    iata: str


@dataclass(frozen=True)
class _Times:
    """The DelayRiskService attributes of a Departure/Arrival row."""
    airport: _Airport
    scheduled_time_utc: str | None = None
    revised_time_utc: str | None = None
    predicted_time_utc: str | None = None
    runway_time_utc: str | None = None


@dataclass(frozen=True)
class PreviewFlight:
    """The DelayRiskService attributes of a Flight, without a database row."""
    id: int
    number: str
    status: str
    departure: _Times
    arrival: _Times


def _descriptor_times(iata: str, times: DelayPreviewTimes) -> _Times:
    return _Times(
        airport=_Airport(iata),
        scheduled_time_utc=times.scheduled_utc,
        revised_time_utc=times.revised_utc,
        predicted_time_utc=times.predicted_utc,
        runway_time_utc=times.runway_utc,
    )


def _row_times(row: Any) -> _Times:
    return _Times(
        airport=_Airport(row.airport.iata.strip().upper()),
        scheduled_time_utc=row.scheduled_time_utc,
        revised_time_utc=row.revised_time_utc,
        predicted_time_utc=row.predicted_time_utc,
        runway_time_utc=row.runway_time_utc,
    )


def _preview_flight(session: Session, request: DelayPreviewRequest, identity: str) -> PreviewFlight | None:
    """The inputs actually scored: the DB row when known, else the descriptor.

    A client descriptor can only influence its own account's single preview.
    It never reaches another account through the shared result cache, whose
    key includes a fingerprint of these inputs.
    """
    descriptor = request.flight
    if request.flight_id is None:
        return PreviewFlight(
            id=0,
            number=_normalized_number(descriptor.number),
            status=descriptor.status.value,
            departure=_descriptor_times(descriptor.departure_iata, descriptor.departure),
            arrival=_descriptor_times(descriptor.arrival_iata, descriptor.arrival),
        )
    row = session.get(Flight, request.flight_id)
    if (
        row is None
        or row.departure is None
        or row.arrival is None
        or row.departure.airport is None
        or row.arrival.airport is None
    ):
        return None
    row_identity = flight_identity(row.number, row.date, row.departure.airport.iata, row.arrival.airport.iata)
    if row_identity is None or row_identity != identity:
        return None
    return PreviewFlight(
        id=row.id,
        number=_normalized_number(row.number),
        status=str(getattr(row.status, "value", row.status)),
        departure=_row_times(row.departure),
        arrival=_row_times(row.arrival),
    )


def _before_departure(flight: PreviewFlight) -> bool:
    return flight.status in PRE_DEPARTURE_STATUSES and not flight.departure.runway_time_utc


def _date_in_window(value: str, now_ms: int) -> bool:
    today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date()
    day = date.fromisoformat(value)
    return today - timedelta(days=DATE_WINDOW_PAST_DAYS) <= day <= today + timedelta(days=DATE_WINDOW_FUTURE_DAYS)


def _fingerprint(flight: PreviewFlight) -> str:
    def times(value: _Times) -> dict:
        return {
            "scheduled": value.scheduled_time_utc,
            "revised": value.revised_time_utc,
            "predicted": value.predicted_time_utc,
            "runway": value.runway_time_utc,
        }

    payload = {"status": flight.status, "departure": times(flight.departure), "arrival": times(flight.arrival)}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- eligibility


def _test_arm_context(session: Session, user_id: str, installation: str) -> ActivationJourneyContext | None:
    """The caller's own server-randomized closable_own_flight assignment."""
    if settings.CLOSABLE_FLOW_FORCE_CONTROL_DELIVERY:
        # Kill switch: test-arm presentations use the control experience.
        return None
    row = session.get(ActivationJourneyAssignment, identity_key(CLOSABLE_FLOW_ID, installation), populate_existing=True)
    if row is None or row.user_id != user_id:
        return None
    try:
        context = ActivationJourneyContext.model_validate_json(row.context_json)
    except ValidationError:
        return None
    if (
        context.experiment_id != CLOSABLE_FLOW_ID
        or context.variant not in (closable_flow.TEST, closable_flow.MATCHED_CONTROL, closable_flow.MATCHED_TEST)
        or context.assignment_source != "server_assignment"
        or str(context.installation_id) != installation
    ):
        return None
    return context


# ---------------------------------------------------------------- storage


@contextmanager
def _write(session: Session):
    """One short SQLite write transaction that waits for the lock up front.

    BEGIN IMMEDIATE takes the write lock before any read, so concurrent
    workers queue on the busy timeout instead of failing a lock upgrade.
    Never held across a provider await.
    """
    session.commit()
    session.exec(text("BEGIN IMMEDIATE"))
    try:
        yield
        session.commit()
    except BaseException:
        session.rollback()
        raise


def _cache_get(session: Session, key: str, now_ms: int) -> Any | None:
    row = session.get(DelayPreviewCache, key, populate_existing=True)
    if row is None or row.expires_at_ms <= now_ms:
        return None
    try:
        return json.loads(row.payload_json)
    except ValueError:
        return None


def _cache_put(session: Session, key: str, payload: Any, ttl_ms: int, now_ms: int) -> None:
    values = {
        "key": key,
        "payload_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        "created_at_ms": now_ms,
        "expires_at_ms": now_ms + ttl_ms,
    }
    session.exec(
        insert(DelayPreviewCache)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[DelayPreviewCache.key],
            set_={key_: values[key_] for key_ in ("payload_json", "created_at_ms", "expires_at_ms")},
        )
    )


def _prune(session: Session, now_ms: int) -> None:
    expired = (
        select(DelayPreviewCache.key)
        .where(DelayPreviewCache.expires_at_ms <= now_ms)
        .limit(PRUNE_LIMIT)
    )
    session.exec(delete(DelayPreviewCache).where(DelayPreviewCache.key.in_(expired)))
    cutoff = (
        datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date() - timedelta(days=BUDGET_RETENTION_DAYS)
    ).isoformat()
    bucket = DelayPreviewBudget.bucket
    old = (
        select(bucket)
        .where(
            or_(
                and_(bucket.like("day:%"), bucket < f"day:{cutoff}"),
                and_(bucket.like("minute:%"), bucket < f"minute:{cutoff}"),
            )
        )
        .limit(PRUNE_LIMIT)
    )
    session.exec(delete(DelayPreviewBudget).where(bucket.in_(old)))


def _consume(session: Session, bucket: str, cap: int) -> bool:
    statement = (
        insert(DelayPreviewBudget)
        .values(bucket=bucket, used=1)
        .on_conflict_do_update(
            index_elements=[DelayPreviewBudget.bucket],
            set_={"used": DelayPreviewBudget.used + 1},
            where=DelayPreviewBudget.used < cap,
        )
    )
    return session.exec(statement).rowcount == 1


def _reserve_build(session: Session, now_ms: int) -> tuple[str, int] | None:
    """Reserve one uncached build in the day and minute buckets, or neither.

    Returns None on success, else (scope, Retry-After seconds). Reservations
    are never refunded: an unknown or failed build keeps its reservation.
    """
    now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    next_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    next_day = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    minute_wait = max(1, math.ceil((next_minute - now).total_seconds()))
    day_wait = max(1, math.ceil((next_day - now).total_seconds()))
    day_cap = settings.DELAY_PREVIEW_DAILY_BUILD_CAP
    minute_cap = settings.DELAY_PREVIEW_MINUTE_BUILD_CAP
    if day_cap <= 0:
        return ("day", day_wait)
    if minute_cap <= 0:
        return ("minute", minute_wait)
    session.commit()
    session.exec(text("BEGIN IMMEDIATE"))
    try:
        if not _consume(session, now.strftime("day:%Y-%m-%d"), day_cap):
            session.rollback()
            return ("day", day_wait)
        if not _consume(session, now.strftime("minute:%Y-%m-%dT%H:%M"), minute_cap):
            session.rollback()
            return ("minute", minute_wait)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return None


# ---------------------------------------------------------------- claim


def _claims(session: Session, user_id: str, installation: str):
    by_user = session.get(DelayPreviewClaim, user_id, populate_existing=True)
    by_installation = session.exec(
        select(DelayPreviewClaim)
        .where(DelayPreviewClaim.installation_id == installation)
        .execution_options(populate_existing=True)
    ).first()
    return by_user, by_installation


def _transition(session: Session, claim: DelayPreviewClaim, **values) -> DelayPreviewClaim | None:
    """Compare-and-swap on the observed revision; None when another request won."""
    owner, revision = claim.user_id, claim.revision
    with _write(session):
        result = session.exec(
            update(DelayPreviewClaim)
            .where(DelayPreviewClaim.user_id == owner, DelayPreviewClaim.revision == revision)
            .values(revision=revision + 1, **values)
        )
        changed = result.rowcount == 1
    if not changed:
        return None
    return session.get(DelayPreviewClaim, owner, populate_existing=True)


def _stored(claim: DelayPreviewClaim) -> PreviewOutcome:
    envelope = json.loads(claim.result_json)
    return _respond(envelope, "stored", outcome="stored")


def _bind(
    session: Session,
    *,
    user_id: str,
    installation: str,
    identity: str,
    flight_id: int | None,
    environment: str,
    now_ms: int,
) -> PreviewOutcome | DelayPreviewClaim:
    """Resolve the one claim for this account and installation.

    Returns an outcome to send as-is, or a claim this request now holds the
    pending lease on and must build.
    """
    for _ in range(3):
        by_user, by_installation = _claims(session, user_id, installation)
        rows = [row for row in (by_user, by_installation) if row is not None]
        if any(row.flight_identity != identity for row in rows):
            return _error(429, "delay_preview_limit_reached", outcome="limit")
        claim = by_user or by_installation
        if claim is None:
            # No conflict target: SQLite applies DO NOTHING to the primary key
            # (account) and the UNIQUE installation alike, so racing workers
            # always end with exactly one binding.
            with _write(session):
                result = session.exec(
                    insert(DelayPreviewClaim)
                    .values(
                        user_id=user_id,
                        installation_id=installation,
                        flight_identity=identity,
                        flight_id=flight_id,
                        state="pending",
                        attempts=0,
                        analytics_environment=environment,
                        pending_until_ms=now_ms + LEASE_MS,
                        created_at_ms=now_ms,
                        updated_at_ms=now_ms,
                        revision=1,
                    )
                    .on_conflict_do_nothing()
                )
                inserted = result.rowcount == 1
            if inserted:
                return session.get(DelayPreviewClaim, user_id, populate_existing=True)
            continue
        if claim.state in FINAL_STATES:
            # Also when this installation's later account reopens the same
            # flight: the stored result, without rebinding.
            return _stored(claim)
        if claim.state == "pending" and (claim.pending_until_ms or 0) > now_ms:
            return _pending(identity)
        # Failed, or a pending lease that expired.
        if claim.attempts >= MAX_BUILDS:
            return _unavailable(retryable=False)
        if claim.failure_reason != "capacity":
            elapsed = now_ms - claim.updated_at_ms
            if elapsed < RETRY_COOLDOWN_MS:
                return _unavailable(retryable=True, retry_after=math.ceil((RETRY_COOLDOWN_MS - elapsed) / 1000))
        lease = _transition(session, claim, state="pending", pending_until_ms=now_ms + LEASE_MS, updated_at_ms=now_ms)
        if lease is None:
            continue
        return lease
    return _pending(identity)


# ---------------------------------------------------------------- provider


_QUIET_PROVIDER_LOGS = contextvars.ContextVar("delay_preview_quiet_provider_logs", default=False)
_LOG_FILTER_NAME = "delay_preview_quiet_provider_logs"


class _QuietPreviewRequests(logging.Filter):
    """httpx logs each request URL at INFO; the preview's carry a flight number or airport."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _QUIET_PROVIDER_LOGS.get()


def _install_log_filter() -> None:
    httpx_logger = logging.getLogger("httpx")
    if not any(getattr(existing, "name", None) == _LOG_FILTER_NAME for existing in httpx_logger.filters):
        httpx_logger.addFilter(_QuietPreviewRequests(_LOG_FILTER_NAME))


_install_log_filter()

_client: httpx.AsyncClient | None = None


def _provider_client() -> httpx.AsyncClient:
    """One client per worker process, created lazily on its event loop.

    The read limit equals the overall deadline: the fetcher starts provider
    calls at 1 per second, so the last of three waits about 2 s before a
    3-4 s provider answer. The overall deadline bounds the whole build.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=PROVIDER_DEADLINE_SECONDS, write=2.0, pool=2.0)
        )
    return _client


def _endpoint(key: str) -> tuple[str, dict[str, str]]:
    kind, value = key.split(":", 1)
    if kind == "history":
        return "flight-delays", {"full_number": value}
    return "airport-delay", {"airport_iata": value}


def _ttl(key: str) -> int:
    return HISTORY_TTL_MS if key.startswith("history:") else AIRPORT_TTL_MS


async def _fetch_json(client: httpx.AsyncClient, key: str) -> tuple[str, Any]:
    path, params = _endpoint(key)
    try:
        response = await client.get(f"{settings.AERODATABOX_SERVICE_URL}{path}", params=params)
    except Exception:
        return "timeout", None
    if response.status_code == 429:
        return "provider_rate_limited", None
    if response.status_code != 200:
        return "provider_unavailable", None
    try:
        # The fetcher maps a provider 204 to {}: success with no data.
        return "ok", response.json()
    except ValueError:
        return "provider_unavailable", None


async def _fetch_missing(keys: list[str]) -> dict[str, tuple[str, Any]]:
    """Outcome-aware calls under one overall deadline. Never raises."""
    token = _QUIET_PROVIDER_LOGS.set(True)
    try:
        client = _provider_client()
        # Tasks copy the current context, so their httpx log records are quiet.
        tasks = {key: asyncio.ensure_future(_fetch_json(client, key)) for key in keys}
    finally:
        _QUIET_PROVIDER_LOGS.reset(token)
    done, pending = await asyncio.wait(tasks.values(), timeout=PROVIDER_DEADLINE_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    results: dict[str, tuple[str, Any]] = {}
    for key, task in tasks.items():
        if task in done and not task.cancelled() and task.exception() is None:
            results[key] = task.result()
        else:
            results[key] = ("timeout", None)
    return results


def _failure_reason(failures: dict[str, str]) -> str:
    reasons = set(failures.values())
    for reason in ("provider_rate_limited", "timeout", "provider_unavailable"):
        if reason in reasons:
            return reason
    return "provider_unavailable"


# ---------------------------------------------------------------- result


_CURRENT_DELAY = re.compile(r"Current (departure|arrival) timing is already -?[0-9]+ min behind schedule\.")
_PROVIDER_STATUS = re.compile(r"AeroDataBox currently marks the flight as [A-Za-z]+\.")
_HISTORY = re.compile(
    r"Historical median delay for this flight is -?[0-9]+ min\."
    r"|This flight has historically run [0-9]+ min early\."
    r"|Historical median delay is close to on time\."
)
_PERCENTILE = re.compile(r"75th percentile historical delay is -?[0-9]+ min\.")
_AIRPORT = re.compile(r"(Departure|Arrival) airport delay index is ([0-9]+(?:\.[0-9]+)?)/5\.")
_SAMPLE = re.compile(r"Historical sample size is limited\.")
_NO_SIGNAL = re.compile(r"No strong delay signals were found from current data\.")


def primary_reason(reasons: list[str]) -> str | None:
    """One returned reason, by the fixed priority the app can localize."""
    for pattern in (_CURRENT_DELAY, _PROVIDER_STATUS, _HISTORY, _PERCENTILE):
        for reason in reasons:
            if pattern.fullmatch(reason):
                return reason
    airports = [(float(match.group(2)), index, reason) for index, reason in enumerate(reasons)
                if (match := _AIRPORT.fullmatch(reason))]
    if airports:
        # Higher index wins; departure (listed first) wins a tie.
        return max(airports, key=lambda item: (item[0], -item[1]))[2]
    for pattern in (_SAMPLE, _NO_SIGNAL):
        for reason in reasons:
            if pattern.fullmatch(reason):
                return reason
    return None


def _has_signal(prediction: dict, status: str) -> bool:
    """A real delay signal exists; "on time now" alone is not a prediction."""
    points = prediction["data_points"]
    return bool(
        points.get("flight_history_available")
        or points.get("airport_delay_available")
        or status == "Delayed"
        or (points.get("current_departure_delay_minutes") or 0) > 5
        or (points.get("current_arrival_delay_minutes") or 0) > 5
    )


def _envelope(identity: str, prediction: DelayRiskResponse, status: str) -> dict:
    data = prediction.model_dump(mode="json")
    if not _has_signal(data, status):
        return {"state": "insufficient_data", "flight_identity": identity, "primary_reason": None, "prediction": None}
    return {"state": "ready", "flight_identity": identity, "primary_reason": primary_reason(data["reasons"]),
            "prediction": data}


def _for_requester(envelope: dict, flight_id: int | None) -> dict:
    """A shared cached result carries the requesting account's own flight ID."""
    envelope = dict(envelope)
    if envelope.get("prediction") is not None:
        envelope["prediction"] = {**envelope["prediction"], "flight_id": flight_id or 0}
    return envelope


def _finish(session: Session, lease: DelayPreviewClaim, envelope: dict, now_ms: int, *, result_key: str | None,
            prune: bool = True) -> None:
    """Persist the final result on the claim, and optionally in the shared cache."""
    owner, revision = lease.user_id, lease.revision
    with _write(session):
        session.exec(
            update(DelayPreviewClaim)
            .where(DelayPreviewClaim.user_id == owner, DelayPreviewClaim.revision == revision)
            .values(
                revision=revision + 1,
                state=envelope["state"],
                result_json=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                failure_reason=None,
                pending_until_ms=None,
                updated_at_ms=now_ms,
            )
        )
        if result_key is not None:
            _cache_put(session, result_key, envelope, RESULT_TTL_MS, now_ms)
        if prune:
            _prune(session, now_ms)


# ---------------------------------------------------------------- build


async def _build(session: Session, lease: DelayPreviewClaim, flight: PreviewFlight, identity: str,
                 flight_id: int | None, now_ms: int) -> PreviewOutcome:
    result_key = f"result:{identity}:{_fingerprint(flight)}"
    cached = _cache_get(session, result_key, now_ms)
    if cached is not None:
        envelope = _for_requester(cached, flight_id)
        _finish(session, lease, envelope, now_ms, result_key=None, prune=False)
        return _respond(envelope, "flight_cache")

    keys = {
        "history": f"history:{flight.number}",
        "departure": f"airport:{flight.departure.airport.iata}",
        "arrival": f"airport:{flight.arrival.airport.iata}",
    }
    inputs = {name: _cache_get(session, key, now_ms) for name, key in keys.items()}
    # Flight history first: it is the strongest signal, and the fetcher starts
    # calls in arrival order at 1 per second, so the last call is the likeliest
    # to miss the deadline when the provider is slow.
    missing = list(dict.fromkeys(keys[name] for name in ("history", "departure", "arrival") if inputs[name] is None))
    failures: dict[str, str] = {}
    provider_calls = 0
    finished_ms = now_ms

    if missing:
        refusal = _reserve_build(session, now_ms)
        if refusal is not None:
            _, retry_after = refusal
            # Attempts unchanged: capacity is not the user's failure.
            _transition(session, lease, state="failed", failure_reason="capacity", pending_until_ms=None,
                        updated_at_ms=now_ms)
            return _error(503, "delay_preview_capacity", outcome="capacity", retryable=True, retry_after=retry_after)
        lease = _transition(session, lease, attempts=lease.attempts + 1, state="pending",
                            pending_until_ms=now_ms + LEASE_MS, updated_at_ms=now_ms)
        if lease is None:
            return _pending(identity)

        fetched = await _fetch_missing(missing)
        provider_calls = len(missing)
        finished_ms = current_time_ms()
        successes = {key: payload for key, (outcome, payload) in fetched.items() if outcome == "ok"}
        failures = {key: outcome for key, (outcome, _) in fetched.items() if outcome != "ok"}
        if successes:
            with _write(session):
                # Only successful payloads are cached; failures never are.
                for key, payload in successes.items():
                    _cache_put(session, key, payload, _ttl(key), finished_ms)
        for name, key in keys.items():
            if inputs[name] is None and key in successes:
                inputs[name] = successes[key]

    prediction = DelayRiskService.compose(
        flight=flight,
        flight_delay_stats=inputs["history"] or {},
        departure_airport_delay=inputs["departure"] or {},
        arrival_airport_delay=inputs["arrival"] or {},
    )
    envelope = _envelope(identity, prediction, flight.status)

    nothing_available = all(value is None for value in inputs.values())
    # Provider inputs only add to the score (apart from a 4-point credit for a
    # historically early flight), so a partial result is a lower bound: a
    # medium or high level stays true, but a "low" may be a failed call read as
    # calm. That is never shown; the successful inputs stay cached, so the
    # retry fetches only what failed.
    reads_low = envelope["state"] == "ready" and envelope["prediction"]["level"] == "low"
    if failures and (nothing_available or envelope["state"] == "insufficient_data" or reads_low):
        # A failed call is never presented as "no signals" or as low risk: the
        # missing input may hold one. The user keeps the retry for this flight.
        reason = _failure_reason(failures)
        failed = _transition(session, lease, state="failed", failure_reason=reason, pending_until_ms=None,
                             updated_at_ms=finished_ms)
        attempts = failed.attempts if failed is not None else lease.attempts
        retryable = attempts < MAX_BUILDS
        return _unavailable(retryable=retryable, retry_after=RETRY_COOLDOWN_MS // 1000,
                            provider_calls=provider_calls)

    # The shared result is written only when every input is real data.
    _finish(session, lease, envelope, finished_ms, result_key=None if failures else result_key)
    return _respond(envelope, "computed", provider_calls=provider_calls)


async def request_delay_preview(*, session: Session, user: Any, payload: Any) -> PreviewOutcome:
    """Validate, check eligibility, bind the claim and build at most once."""
    if not settings.DELAY_PREVIEW_ENABLED:
        return _error(503, "delay_preview_disabled", outcome="disabled")
    user_id = user.id
    try:
        request = DelayPreviewRequest.model_validate(payload)
    except ValidationError:
        return _invalid()
    now_ms = current_time_ms()
    descriptor = request.flight
    identity = flight_identity(descriptor.number, descriptor.date, descriptor.departure_iata,
                               descriptor.arrival_iata)
    if identity is None or identity != request.flight_identity or not _date_in_window(descriptor.date, now_ms):
        return _invalid()
    installation = str(request.installation_id).lower()

    context = _test_arm_context(session, user_id, installation)
    if context is None:
        return _error(403, "delay_preview_not_eligible", outcome="not_eligible")

    flight = _preview_flight(session, request, identity)
    if flight is None:
        return _invalid()
    if not _before_departure(flight):
        return _error(422, "delay_preview_flight_not_eligible", outcome="flight_not_eligible")

    bound = _bind(
        session,
        user_id=user_id,
        installation=installation,
        identity=identity,
        flight_id=request.flight_id,
        environment=context.analytics_environment,
        now_ms=now_ms,
    )
    if isinstance(bound, PreviewOutcome):
        return bound
    return await _build(session, bound, flight, identity, request.flight_id, now_ms)
