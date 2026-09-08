"""Credential-free constants shared by request handling and the cleanup command."""

RETENTION_DAYS = 7
RETENTION_MS = RETENTION_DAYS * 24 * 60 * 60 * 1_000
# Expire before the public upper bound, leaving room for the one-minute sweep.
# This is operating headroom, not an outage-proof physical-deletion guarantee.
CLEANUP_HEADROOM_MS = 10 * 60 * 1_000
SAMPLE_RETENTION_MS = RETENTION_MS - CLEANUP_HEADROOM_MS
CLEANUP_INTERVAL_SECONDS = 60
CLEANUP_STATUS_STALE_MS = 2 * CLEANUP_INTERVAL_SECONDS * 1_000
DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 2_000
CLEANUP_OUTCOMES = frozenset({
    "never_run", "running", "success", "bounded_backlog", "checkpoint_busy",
    "cleanup_failed", "deadline_exceeded",
})


def effective_expiry_ms(*, created_at_ms: int, expires_at_ms: int) -> int:
    """Old rows with retry-extended expiries are subject to the same capture cap."""
    return min(expires_at_ms, created_at_ms + SAMPLE_RETENTION_MS)


def sample_is_live(*, created_at_ms: int, expires_at_ms: int, now_ms: int) -> bool:
    return created_at_ms <= now_ms < effective_expiry_ms(
        created_at_ms=created_at_ms, expires_at_ms=expires_at_ms
    )
