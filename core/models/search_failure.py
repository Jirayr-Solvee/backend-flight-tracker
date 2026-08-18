from time import time

from sqlmodel import Field, SQLModel


def current_time_ms() -> int:
    return int(time() * 1_000)


class SearchFailureSample(SQLModel, table=True):
    id: str = Field(primary_key=True)
    user_hash: str = Field(index=True)
    query_ciphertext: str
    query_digest: str = Field(index=True)

    source: str = Field(index=True)
    query_type: str = Field(index=True)
    failure_reason: str = Field(index=True)
    provider_outcome: str = Field(index=True)
    normalization_applied: bool
    provider_result_count: int
    filtered_result_count: int = 0
    provider_latency_ms: int | None = None

    search_journey_id: str | None = Field(default=None, index=True)
    search_attempt_number: int | None = None
    app_version: str | None = Field(default=None, index=True)
    build_number: str | None = None
    analytics_environment: str = Field(default="unknown", index=True)

    airline_iata: str | None = None
    flight_number: str | None = None
    departure_airport_iata: str | None = None
    arrival_airport_iata: str | None = None
    airport_iata: str | None = None
    departure_date: str | None = None
    direction: str | None = None

    created_at_ms: int = Field(default_factory=current_time_ms, index=True)
    last_reported_at_ms: int = Field(default_factory=current_time_ms)
    expires_at_ms: int = Field(index=True)
