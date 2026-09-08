#!/usr/bin/env python3
"""Bounded, credential-free cleanup of expired failed-search samples only.

Read-only by default. This module deliberately does not import the `core`
package: its initializer opens the application database and loads app models.
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote


POLICY_DIRECTORY = Path(__file__).resolve().parents[1] / "core"
sys.path.insert(0, str(POLICY_DIRECTORY))
from search_failure_retention_policy import (  # noqa: E402
    CLEANUP_STATUS_STALE_MS,
    CLEANUP_OUTCOMES,
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    SAMPLE_RETENTION_MS,
)


STATUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS searchfailurecleanupstatus (
    id INTEGER NOT NULL PRIMARY KEY,
    outcome VARCHAR NOT NULL,
    last_started_at_ms INTEGER,
    last_finished_at_ms INTEGER,
    last_success_at_ms INTEGER,
    last_deleted_count INTEGER NOT NULL DEFAULT 0,
    last_clamped_count INTEGER NOT NULL DEFAULT 0,
    expired_remaining INTEGER,
    oldest_expired_at_ms INTEGER
)
"""


def _now_ms() -> int:
    return int(time.time() * 1_000)


def _aggregate(connection: sqlite3.Connection, cutoff: int) -> tuple[int, int | None]:
    row = connection.execute(
        "SELECT COUNT(*), MIN(MIN(expires_at_ms, created_at_ms + ?)) "
        "FROM searchfailuresample WHERE expires_at_ms <= ? OR created_at_ms <= ?",
        (SAMPLE_RETENTION_MS, cutoff, cutoff - SAMPLE_RETENTION_MS),
    ).fetchone()
    return int(row[0]), int(row[1]) if row[1] is not None else None


def _last_status(connection: sqlite3.Connection) -> tuple[int | None, str]:
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='searchfailurecleanupstatus'"
    ).fetchone() is None:
        return None, "never_run"
    row = connection.execute(
        "SELECT last_success_at_ms, outcome FROM searchfailurecleanupstatus WHERE id=1"
    ).fetchone()
    if row is None:
        return None, "never_run"
    return (int(row[0]) if row[0] is not None else None,
            row[1] if row[1] in CLEANUP_OUTCOMES else "invalid_status")


def _save_status(connection: sqlite3.Connection, result: dict) -> None:
    """Persist aggregate counters only; never persist exception text or row IDs."""
    connection.execute(
        "INSERT INTO searchfailurecleanupstatus "
        "(id, outcome, last_started_at_ms, last_finished_at_ms, last_success_at_ms, "
        "last_deleted_count, last_clamped_count, expired_remaining, oldest_expired_at_ms) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "outcome=excluded.outcome, last_started_at_ms=excluded.last_started_at_ms, "
        "last_finished_at_ms=COALESCE(excluded.last_finished_at_ms, searchfailurecleanupstatus.last_finished_at_ms), "
        "last_success_at_ms=CASE WHEN excluded.last_success_at_ms IS NULL "
        "THEN searchfailurecleanupstatus.last_success_at_ms "
        "ELSE MAX(COALESCE(searchfailurecleanupstatus.last_success_at_ms, 0), excluded.last_success_at_ms) END, "
        "last_deleted_count=excluded.last_deleted_count, "
        "last_clamped_count=excluded.last_clamped_count, "
        "expired_remaining=excluded.expired_remaining, oldest_expired_at_ms=excluded.oldest_expired_at_ms",
        (
            result["outcome"], result["started_at_ms"], result["finished_at_ms"],
            result["finished_at_ms"] if result["outcome"] == "success" else None,
            result["deleted_count"], result["clamped_count"],
            result["expired_remaining"], result["oldest_expired_at_ms"],
        ),
    )


def run_cleanup(
    database: Path,
    *,
    apply: bool = False,
    status_only: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = 40,
    deadline_seconds: float = 20,
    now_ms: int | None = None,
) -> dict:
    """Never create a database or modify unrelated tables.

    `now_ms` exists only for deterministic synthetic tests, not as a CLI option.
    Each delete transaction obtains its cutoff after acquiring SQLite's writer
    lock. A failed/bounded run does not advance the last-successful-sweep fact.
    """
    clock = _now_ms if now_ms is None else lambda: now_ms
    result = {
        "outcome": "invalid_arguments", "applied": apply,
        "started_at_ms": clock(), "finished_at_ms": None,
        "deleted_count": 0, "clamped_count": 0, "batches": 0,
        "expired_remaining": None, "oldest_expired_at_ms": None,
        "last_success_at_ms": None, "last_success_stale": True,
        "last_sweep_outcome": "never_run",
    }
    if (not 1 <= batch_size <= MAX_BATCH_SIZE or not 1 <= max_batches <= 100
            or not 0 < deadline_seconds <= 30 or (apply and status_only)):
        return result

    connection = None
    deadline = time.monotonic() + deadline_seconds
    try:
        target = Path(database).resolve(strict=True)
        if not target.is_file():
            result["outcome"] = "database_unavailable"
            return result
        connection = sqlite3.connect(
            f"file:{quote(target.as_posix(), safe='/')}?mode={'rw' if apply else 'ro'}",
            uri=True, timeout=min(1.0, deadline_seconds), isolation_level=None,
        )
        # Bound expensive statements as well as the Python batch loop.
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1_000)
        required_columns = {"id", "user_hash", "query_ciphertext", "query_digest", "created_at_ms", "expires_at_ms"}
        actual_columns = {row[1] for row in connection.execute("PRAGMA table_info(searchfailuresample)")}
        if not required_columns <= actual_columns:
            result["outcome"] = "search_sample_schema_missing"
            return result
        result["last_success_at_ms"], result["last_sweep_outcome"] = _last_status(connection)
        result["expired_remaining"], result["oldest_expired_at_ms"] = _aggregate(connection, clock())
        if not apply:
            result["outcome"] = "status" if status_only else "dry_run"
            return result

        connection.execute("PRAGMA secure_delete=ON")
        connection.execute(STATUS_SCHEMA)
        result["outcome"] = "running"
        _save_status(connection, result)

        for _ in range(max_batches):
            if time.monotonic() >= deadline:
                result["outcome"] = "deadline_exceeded"
                break
            connection.execute("BEGIN IMMEDIATE")
            cutoff = clock()
            deleted = connection.execute(
                "DELETE FROM searchfailuresample WHERE id IN ("
                "SELECT id FROM searchfailuresample "
                "WHERE expires_at_ms <= ? OR created_at_ms <= ? "
                "ORDER BY expires_at_ms, id LIMIT ?)",
                (cutoff, cutoff - SAMPLE_RETENTION_MS, batch_size),
            ).rowcount
            # Clamp still-live legacy rows whose retries formerly extended the
            # clock. This changes expiry only, never ciphertext/capture context.
            clamped = connection.execute(
                "UPDATE searchfailuresample SET expires_at_ms=created_at_ms + ? "
                "WHERE id IN (SELECT id FROM searchfailuresample "
                "WHERE expires_at_ms > created_at_ms + ? AND expires_at_ms > ? "
                "AND created_at_ms > ? ORDER BY created_at_ms, id LIMIT ?)",
                (SAMPLE_RETENTION_MS, SAMPLE_RETENTION_MS, cutoff,
                 cutoff - SAMPLE_RETENTION_MS, batch_size),
            ).rowcount
            connection.commit()
            result["deleted_count"] += deleted
            result["clamped_count"] += clamped
            result["batches"] += 1
            if deleted < batch_size and clamped < batch_size:
                break

        result["expired_remaining"], result["oldest_expired_at_ms"] = _aggregate(connection, clock())
        unclamped = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM searchfailuresample WHERE expires_at_ms > created_at_ms + ?)",
            (SAMPLE_RETENTION_MS,),
        ).fetchone()[0]
        if result["outcome"] != "deadline_exceeded":
            result["outcome"] = "bounded_backlog" if result["expired_remaining"] or unclamped else "success"

        # WAL can retain older encrypted page versions even after a secure
        # delete. Do not claim sweep success while an active reader prevents
        # truncation. Never change journal_mode or vacuum unrelated tables.
        if connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint[0] != 0:
                result["outcome"] = "checkpoint_busy"

        connection.execute("BEGIN IMMEDIATE")
        result["expired_remaining"], result["oldest_expired_at_ms"] = _aggregate(connection, clock())
        if result["expired_remaining"] and result["outcome"] == "success":
            result["outcome"] = "bounded_backlog"
        result["finished_at_ms"] = clock()
        _save_status(connection, result)
        connection.commit()
        result["last_success_at_ms"], result["last_sweep_outcome"] = _last_status(connection)
    except FileNotFoundError:
        result["outcome"] = "database_unavailable"
    except Exception:
        # Driver/configuration errors can include sensitive values. Neither the
        # console nor operational status may contain exception text/tracebacks.
        result["outcome"] = "deadline_exceeded" if time.monotonic() >= deadline else "cleanup_failed"
        if connection is not None:
            try:
                connection.rollback()
                result["finished_at_ms"] = clock()
                if apply and time.monotonic() < deadline:
                    _save_status(connection, result)
                    result["last_success_at_ms"], result["last_sweep_outcome"] = _last_status(connection)
            except Exception:
                pass
    finally:
        result["finished_at_ms"] = clock()
        last_success = result["last_success_at_ms"]
        result["last_success_stale"] = (
            last_success is None or last_success > result["finished_at_ms"]
            or result["finished_at_ms"] - last_success > CLEANUP_STATUS_STALE_MS
        )
        if connection is not None:
            try:
                connection.close()
            except Exception:
                result["outcome"] = "cleanup_failed"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Delete expired search samples; default is read-only")
    parser.add_argument("--status", action="store_true", help="Read aggregate expiry and last-success status only")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=40)
    parser.add_argument("--deadline-seconds", type=float, default=20)
    args = parser.parse_args()
    result = run_cleanup(
        args.database, apply=args.apply, status_only=args.status,
        batch_size=args.batch_size, max_batches=args.max_batches,
        deadline_seconds=args.deadline_seconds,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["outcome"] in {"success", "dry_run", "status"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
