"""Onboarding delay preview: one real prediction per account and installation.

Shared by every API worker through the application database. A claim links an
account to a hashed flight identity and an installation ID (usage data linked
to the user); it is deleted with the account. Cache rows hold provider payloads
and composed results only, never account or installation data.
"""

from sqlmodel import Field, SQLModel


class DelayPreviewClaim(SQLModel, table=True):
    """One onboarding delay preview per account and per installation, ever."""
    user_id: str = Field(primary_key=True)
    # Lowercase closable-flow installation UUID. UNIQUE: a second account on
    # the same installation shares this claim instead of getting another one.
    installation_id: str = Field(unique=True, index=True)
    # 64-hex FlightFunnelIdentityPolicy hash recomputed by the server.
    flight_identity: str
    flight_id: int | None = None
    state: str  # pending | ready | insufficient_data | failed
    attempts: int = 0  # uncached builds started, at most 2
    failure_reason: str | None = None  # capacity | provider_unavailable | provider_rate_limited | timeout
    result_json: str | None = None  # response envelope without account/installation data
    analytics_environment: str  # copied from the assignment context
    pending_until_ms: int | None = None
    created_at_ms: int
    updated_at_ms: int
    # Compare-and-swap guard: every state change requires the observed revision.
    revision: int = 1


class DelayPreviewCache(SQLModel, table=True):
    """Shared, expiring provider inputs and composed results."""
    key: str = Field(primary_key=True)  # result:<identity>:<fp16> | airport:<IATA> | history:<NUMBER>
    payload_json: str
    created_at_ms: int
    expires_at_ms: int = Field(index=True)


class DelayPreviewBudget(SQLModel, table=True):
    """Global uncached-build reservations. Never refunded."""
    bucket: str = Field(primary_key=True)  # day:YYYY-MM-DD | minute:YYYY-MM-DDTHH:MM (UTC)
    used: int = 0
