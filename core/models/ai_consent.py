"""Account-owned, explicit AI permissions; absence is always a denial."""

from sqlmodel import Field, SQLModel


class UserAIConsent(SQLModel, table=True):
    user_id: str = Field(primary_key=True, foreign_key="user.id")
    policy_version: int = Field(default=1, sa_column_kwargs={"server_default": "1"})
    revision: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    search_enabled: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})
    forwarded_email_enabled: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})
    # A fresh email Allow creates a new generation. Receipt arrival must follow
    # this grant; later regrant cannot revive a queued/replayed older message.
    forwarded_email_grant_id: str | None = None
    forwarded_email_granted_at_ms: int | None = None
    updated_at: int | None = None


class AIConsentReceipt(SQLModel, table=True):
    user_id: str = Field(primary_key=True, foreign_key="user.id")
    request_id: str = Field(primary_key=True)
    policy_version: int
    expected_revision: int
    purpose: str
    enabled: bool


class UserAIEmailIdentity(SQLModel, table=True):
    # Historical User.email accepted client input, not necessarily an
    # Apple-verified address. Never backfill this proof from legacy rows.
    user_id: str = Field(primary_key=True, foreign_key="user.id")
    apple_id: str
    verified_email: str = Field(index=True)


class UserAIEmailReceipt(SQLModel, table=True):
    # Global at-most-once tombstones survive account deletion. No raw email,
    # sender, message ID, object key, account ID or grant ID is persisted here.
    receipt_digest: str = Field(primary_key=True)
    proof_digest: str
    owner_binding: str
    received_at_ms: int
    claimed_at_ms: int
    state: str = Field(default="processing", sa_column_kwargs={"server_default": "processing"})
    finished_at_ms: int | None = None
