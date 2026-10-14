"""Secret, revocable public links to one tracked flight; no viewer identity."""

from time import time

from sqlalchemy import Index, text
from sqlmodel import Field, SQLModel


def current_time_ms() -> int:
    return int(time() * 1_000)


class FlightShare(SQLModel, table=True):
    __tablename__ = "flight_share"
    # One active link per owner and flight. Revoked rows keep their history and
    # no longer block a later share, which gets a new id and so a new token.
    __table_args__ = (
        Index(
            "ux_flight_share_active", "user_id", "flight_id",
            unique=True,
            sqlite_where=text("revoked_at_ms IS NULL"),
        ),
    )

    id: str = Field(primary_key=True)
    # SHA-256 of the raw token. The token itself is never stored; it is
    # recomputed from the id with a key derived from JWT_SECRET.
    token_hash: str = Field(unique=True, index=True)
    # No foreign keys: a revoked row outlives the flight link and the account.
    user_id: str = Field(index=True)
    flight_id: int = Field(index=True)
    created_at_ms: int = Field(default_factory=current_time_ms)
    revoked_at_ms: int | None = None
    revoke_reason: str | None = None  # owner | flight_removed | account_deleted
    # Aggregate only: no viewer IP, user agent or referrer is retained.
    view_count: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    last_viewed_at_ms: int | None = None
