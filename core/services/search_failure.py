import base64
import hashlib
import hmac
import re
from time import time
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, or_, text, update
from sqlmodel import Session, select

from ..config import settings
from ..models.search_failure import SearchFailureCleanupStatus, SearchFailureSample
from ..search_failure_retention_policy import (
    CLEANUP_STATUS_STALE_MS,
    CLEANUP_OUTCOMES,
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    RETENTION_DAYS,
    RETENTION_MS,
    SAMPLE_RETENTION_MS,
    effective_expiry_ms,
    sample_is_live,
)


MAX_QUERY_LENGTH = 2_000


class SearchFailureService:
    _allowed_structured_fields = {
        "airline_iata",
        "flight_number",
        "departure_airport_iata",
        "arrival_airport_iata",
        "airport_iata",
        "departure_date",
        "direction",
    }

    @classmethod
    def _fernet(cls) -> Fernet:
        digest = hashlib.sha256(
            f"{settings.JWT_SECRET}:search-failures:v1".encode("utf-8")
        ).digest()
        return Fernet(base64.urlsafe_b64encode(digest))

    @staticmethod
    def _redact_query(query: str) -> str:
        value = query[:MAX_QUERY_LENGTH]
        value = re.sub(
            r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
            "[email]",
            value,
        )
        value = re.sub(r"(?i)https?://\S+", "[url]", value)
        value = re.sub(
            r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)",
            "[phone]",
            value,
        )
        return re.sub(r"\s+", " ", value).strip()

    @classmethod
    def _encrypt_query(cls, query: str) -> str:
        redacted = cls._redact_query(query)
        return cls._fernet().encrypt(redacted.encode("utf-8")).decode("ascii")

    @classmethod
    def decrypt_query(
        cls,
        ciphertext: str,
        *,
        created_at_ms: int,
        expires_at_ms: int,
        now_ms: int | None = None,
    ) -> str | None:
        # The caller must supply the original capture boundary. Even a report
        # that selected this row just before expiry must not decrypt it later.
        cutoff = now_ms if now_ms is not None else int(time() * 1_000)
        if not sample_is_live(
            created_at_ms=created_at_ms, expires_at_ms=expires_at_ms, now_ms=cutoff
        ):
            return None
        try:
            redacted = cls._fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
            if not sample_is_live(
                created_at_ms=created_at_ms,
                expires_at_ms=expires_at_ms,
                now_ms=now_ms if now_ms is not None else int(time() * 1_000),
            ):
                return None
            return redacted
        except (InvalidToken, UnicodeDecodeError, ValueError):
            return None

    @staticmethod
    def _keyed_digest(value: str, *, purpose: str) -> str:
        return hmac.new(
            settings.JWT_SECRET.encode("utf-8"),
            f"{purpose}:{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]

    @classmethod
    def user_hash(cls, user_id: str) -> str:
        return cls._keyed_digest(user_id, purpose="user")

    @classmethod
    def query_digest(cls, query: str) -> str:
        normalized = re.sub(r"\s+", " ", query).strip().casefold()
        return cls._keyed_digest(normalized, purpose="query")

    @classmethod
    def purge_expired(
        cls,
        session: Session,
        *,
        now_ms: int | None = None,
        limit: int = DEFAULT_BATCH_SIZE,
    ) -> int:
        if not 1 <= limit <= MAX_BATCH_SIZE:
            raise ValueError("invalid_search_cleanup_batch_size")
        cutoff = now_ms if now_ms is not None else int(time() * 1_000)
        # The production store is SQLite. Overwrite deleted cells instead of
        # leaving their contents in its reusable database pages. The independent
        # cleanup command also checkpoints WAL when that journal mode is used.
        session.exec(text("PRAGMA secure_delete=ON"))
        expired_ids = (
            select(SearchFailureSample.id)
            .where(
                or_(
                    SearchFailureSample.expires_at_ms <= cutoff,
                    SearchFailureSample.created_at_ms <= cutoff - SAMPLE_RETENTION_MS,
                )
            )
            .order_by(SearchFailureSample.expires_at_ms, SearchFailureSample.id)
            .limit(limit)
        )
        result = session.exec(
            delete(SearchFailureSample).where(SearchFailureSample.id.in_(expired_ids))
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    @classmethod
    def structured_values(cls, args: dict[str, Any] | None) -> dict[str, str | None]:
        values: dict[str, str | None] = {
            key: None for key in cls._allowed_structured_fields
        }
        for key in cls._allowed_structured_fields:
            raw_value = (args or {}).get(key)
            if raw_value is None:
                continue
            value = str(raw_value).strip()[:40]
            values[key] = value or None
        return values

    @classmethod
    def record(
        cls,
        *,
        session: Session,
        user_id: str,
        query: str,
        source: str,
        query_type: str,
        failure_reason: str,
        provider_outcome: str,
        normalization_applied: bool,
        provider_result_count: int,
        filtered_result_count: int = 0,
        provider_latency_ms: int | None = None,
        search_journey_id: str | None = None,
        search_attempt_number: int | None = None,
        app_version: str | None = None,
        build_number: str | None = None,
        analytics_environment: str = "unknown",
        structured_args: dict[str, Any] | None = None,
        sample_id: str | None = None,
        allow_new_capture: bool = False,
    ) -> SearchFailureSample | None:
        # Only the actual backend search handler opts into a fresh capture.
        # An app payload's free-form `source` is never authority to create one.
        # Legacy no-ID reports cannot prove freshness after deletion, so their
        # best-effort HTTP acknowledgement must not retain the query again.
        if sample_id is None and not allow_new_capture:
            return None
        expected_user_hash = cls.user_hash(user_id)

        if sample_id is not None:
            # A write statement acquires SQLite's writer lock before the read,
            # serializing independent API workers and the cleanup process. A
            # fresh clock AFTER lock acquisition is essential at the deadline.
            session.exec(
                update(SearchFailureSample)
                .where(
                    SearchFailureSample.id == sample_id,
                    SearchFailureSample.user_hash == expected_user_hash,
                )
                .values(last_reported_at_ms=SearchFailureSample.last_reported_at_ms)
                .execution_options(synchronize_session=False)
            )
            now_ms = int(time() * 1_000)
            existing = session.exec(
                select(SearchFailureSample)
                .where(
                    SearchFailureSample.id == sample_id,
                    SearchFailureSample.user_hash == expected_user_hash,
                )
                .execution_options(populate_existing=True)
            ).first()
            if existing is None:
                return None
            expiry_ms = effective_expiry_ms(
                created_at_ms=existing.created_at_ms, expires_at_ms=existing.expires_at_ms
            )
            if not sample_is_live(
                created_at_ms=existing.created_at_ms,
                expires_at_ms=existing.expires_at_ms,
                now_ms=now_ms,
            ):
                # Never turn a supplied expired, missing, or foreign ID into a
                # new sample. Independent cleanup owns physical row removal.
                return None
            if existing.query_digest != cls.query_digest(query):
                return None
            if (
                existing.search_journey_id is not None
                and search_journey_id is not None
                and existing.search_journey_id != search_journey_id
            ) or (
                existing.search_attempt_number is not None
                and search_attempt_number is not None
                and existing.search_attempt_number != search_attempt_number
            ):
                return None

            if not allow_new_capture or existing.source != source:
                existing.source = "backend_and_app"
            if not (
                failure_reason in {"provider_no_match", "unknown"}
                and existing.failure_reason not in {"provider_no_match", "unknown"}
            ):
                # Older clients collapse unfamiliar recovery reasons into the
                # generic provider_no_match value. Keep a more precise backend
                # classification, while still allowing client-only outcomes
                # such as landed_only to replace a generic backend reason.
                existing.failure_reason = (failure_reason or existing.failure_reason)[:100]
            existing.normalization_applied = (
                existing.normalization_applied or normalization_applied
            )
            existing.provider_result_count = max(
                existing.provider_result_count,
                max(0, provider_result_count),
            )
            existing.filtered_result_count = max(
                existing.filtered_result_count,
                max(0, filtered_result_count),
            )
            # App/build/environment and provider facts describe the backend
            # capture, even when originally unknown. A later app report cannot
            # establish those historical values. Journey/attempt are separate
            # one-time correlation bindings, not replacement capture metadata.
            existing.search_journey_id = existing.search_journey_id or search_journey_id
            if existing.search_attempt_number is None:
                existing.search_attempt_number = search_attempt_number
            existing.last_reported_at_ms = max(existing.last_reported_at_ms, now_ms)
            existing.expires_at_ms = expiry_ms
            session.add(existing)
            return existing

        now_ms = int(time() * 1_000)
        structured = cls.structured_values(structured_args)
        sample = SearchFailureSample(
            id=str(uuid4()),
            user_hash=expected_user_hash,
            query_ciphertext=cls._encrypt_query(query),
            query_digest=cls.query_digest(query),
            source="backend",
            query_type=(query_type or "unknown")[:80],
            failure_reason=(failure_reason or "unknown")[:100],
            provider_outcome=(provider_outcome or "unknown")[:100],
            normalization_applied=normalization_applied,
            provider_result_count=max(0, provider_result_count),
            filtered_result_count=max(0, filtered_result_count),
            provider_latency_ms=(
                max(0, provider_latency_ms)
                if provider_latency_ms is not None
                else None
            ),
            search_journey_id=search_journey_id,
            search_attempt_number=search_attempt_number,
            app_version=app_version,
            build_number=build_number,
            analytics_environment=analytics_environment[:20],
            created_at_ms=now_ms,
            last_reported_at_ms=now_ms,
            expires_at_ms=now_ms + SAMPLE_RETENTION_MS,
            **structured,
        )
        session.add(sample)
        return sample

    @staticmethod
    def recent(
        session: Session,
        *,
        since_ms: int,
        limit: int,
        now_ms: int | None = None,
    ) -> list[SearchFailureSample]:
        cutoff = now_ms if now_ms is not None else int(time() * 1_000)
        statement = (
            select(SearchFailureSample)
            .where(
                SearchFailureSample.created_at_ms >= since_ms,
                SearchFailureSample.created_at_ms <= cutoff,
                SearchFailureSample.created_at_ms > cutoff - SAMPLE_RETENTION_MS,
                SearchFailureSample.expires_at_ms > cutoff,
            )
            .order_by(SearchFailureSample.created_at_ms.desc())  # type: ignore[attr-defined]
            .limit(limit)
        )
        return list(session.exec(statement).all())

    @staticmethod
    def cleanup_status(session: Session, *, now_ms: int | None = None) -> dict[str, Any]:
        cutoff = now_ms if now_ms is not None else int(time() * 1_000)
        stored = session.get(SearchFailureCleanupStatus, 1)
        last_success = stored.last_success_at_ms if stored else None
        outcome = stored.outcome if stored else "never_run"
        return {
            "outcome": outcome if outcome in CLEANUP_OUTCOMES else "invalid_status",
            "last_started_at_ms": stored.last_started_at_ms if stored else None,
            "last_finished_at_ms": stored.last_finished_at_ms if stored else None,
            "last_success_at_ms": last_success,
            "last_deleted_count": stored.last_deleted_count if stored else 0,
            "last_clamped_count": stored.last_clamped_count if stored else 0,
            "expired_remaining": stored.expired_remaining if stored else None,
            "oldest_expired_at_ms": stored.oldest_expired_at_ms if stored else None,
            "stale": (
                last_success is None
                or last_success > cutoff
                or cutoff - last_success > CLEANUP_STATUS_STALE_MS
            ),
        }
