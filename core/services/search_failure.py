import base64
import hashlib
import hmac
import re
from time import time
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete
from sqlmodel import Session, select

from ..config import settings
from ..models.search_failure import SearchFailureSample


RETENTION_DAYS = 7
RETENTION_MS = RETENTION_DAYS * 24 * 60 * 60 * 1_000
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
    def decrypt_query(cls, ciphertext: str) -> str | None:
        try:
            return cls._fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
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
    def purge_expired(cls, session: Session, *, now_ms: int | None = None) -> int:
        cutoff = now_ms if now_ms is not None else int(time() * 1_000)
        result = session.exec(
            delete(SearchFailureSample).where(
                SearchFailureSample.expires_at_ms <= cutoff
            )
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
    ) -> SearchFailureSample:
        now_ms = int(time() * 1_000)
        cls.purge_expired(session, now_ms=now_ms)
        expected_user_hash = cls.user_hash(user_id)

        existing = session.get(SearchFailureSample, sample_id) if sample_id else None
        if existing and existing.user_hash == expected_user_hash:
            existing.source = (
                "backend_and_app" if existing.source != source else existing.source
            )
            existing.query_type = query_type or existing.query_type
            existing.failure_reason = failure_reason or existing.failure_reason
            existing.provider_outcome = provider_outcome or existing.provider_outcome
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
            existing.provider_latency_ms = (
                provider_latency_ms
                if provider_latency_ms is not None
                else existing.provider_latency_ms
            )
            existing.search_journey_id = search_journey_id or existing.search_journey_id
            existing.search_attempt_number = (
                search_attempt_number
                if search_attempt_number is not None
                else existing.search_attempt_number
            )
            existing.app_version = app_version or existing.app_version
            existing.build_number = build_number or existing.build_number
            if analytics_environment != "unknown":
                existing.analytics_environment = analytics_environment
            existing.last_reported_at_ms = now_ms
            existing.expires_at_ms = max(existing.expires_at_ms, now_ms + RETENTION_MS)
            session.add(existing)
            return existing

        structured = cls.structured_values(structured_args)
        sample = SearchFailureSample(
            id=str(uuid4()),
            user_hash=expected_user_hash,
            query_ciphertext=cls._encrypt_query(query),
            query_digest=cls.query_digest(query),
            source=source,
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
            expires_at_ms=now_ms + RETENTION_MS,
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
    ) -> list[SearchFailureSample]:
        statement = (
            select(SearchFailureSample)
            .where(SearchFailureSample.created_at_ms >= since_ms)
            .order_by(SearchFailureSample.created_at_ms.desc())  # type: ignore[attr-defined]
            .limit(limit)
        )
        return list(session.exec(statement).all())
