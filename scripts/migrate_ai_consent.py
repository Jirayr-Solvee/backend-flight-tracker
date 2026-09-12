#!/usr/bin/env python3
"""Add explicit AI consent storage, never opting existing accounts in.

Usage: python scripts/migrate_ai_consent.py /exact/existing/database.db
Only AI storage is created/upgraded; user records are not changed/backfilled.
Fresh installations also create these tables via SQLModel.metadata.create_all.
This migration is idempotent and does not load application settings.
"""

import argparse
import re
import sqlite3
from contextlib import closing
from pathlib import Path


TABLES = {
    "useraiconsent": """
        CREATE TABLE IF NOT EXISTS useraiconsent (
            user_id VARCHAR NOT NULL PRIMARY KEY REFERENCES user(id),
            policy_version INTEGER NOT NULL DEFAULT 1,
            revision INTEGER NOT NULL DEFAULT 0,
            search_enabled BOOLEAN NOT NULL DEFAULT 0,
            forwarded_email_enabled BOOLEAN NOT NULL DEFAULT 0,
            forwarded_email_grant_id VARCHAR,
            forwarded_email_granted_at_ms INTEGER,
            updated_at INTEGER
        )
    """,
    "aiconsentreceipt": """
        CREATE TABLE IF NOT EXISTS aiconsentreceipt (
            user_id VARCHAR NOT NULL REFERENCES user(id),
            request_id VARCHAR NOT NULL,
            policy_version INTEGER NOT NULL,
            expected_revision INTEGER NOT NULL,
            purpose VARCHAR NOT NULL,
            enabled BOOLEAN NOT NULL,
            PRIMARY KEY (user_id, request_id)
        )
    """,
    "useraiemailidentity": """
        CREATE TABLE IF NOT EXISTS useraiemailidentity (
            user_id VARCHAR NOT NULL PRIMARY KEY REFERENCES user(id),
            apple_id VARCHAR NOT NULL,
            verified_email VARCHAR NOT NULL
        )
    """,
    "useraiemailreceipt": """
        CREATE TABLE IF NOT EXISTS useraiemailreceipt (
            receipt_digest VARCHAR NOT NULL PRIMARY KEY,
            proof_digest VARCHAR NOT NULL,
            owner_binding VARCHAR NOT NULL,
            received_at_ms INTEGER NOT NULL,
            claimed_at_ms INTEGER NOT NULL,
            state VARCHAR NOT NULL DEFAULT 'processing',
            finished_at_ms INTEGER
        )
    """,
}
EMAIL_INDEX = "ix_useraiemailidentity_verified_email"
GRANT_COLUMNS = {
    "forwarded_email_grant_id": "VARCHAR",
    "forwarded_email_granted_at_ms": "INTEGER",
}


def _default(value: str | None) -> str | None:
    """Compare literal defaults, including SQLModel's quoted integer defaults."""
    if value is None:
        return None
    value = value.strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    if value in {"'0'", '"0"', "'1'", '"1"'}:
        value = value[1:-1]
    return value


def _tokens(sql: str) -> list[str]:
    # Preserve string literals; ignore comments and normalize quoted identifiers.
    parts = re.findall(
        r"--[^\n]*|/\*[\s\S]*?\*/|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|"
        r"`(?:[^`]|``)*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|[^\s]", sql,
    )
    return [
        part if part.startswith("'") else (
            part[1:-1].replace(part[0] * 2, part[0]).lower()
            if part[0] in {'"', '`', '['} else part.lower()
        )
        for part in parts if not part.startswith(("--", "/*"))
    ]


def _columns(connection: sqlite3.Connection, table: str) -> dict:
    return {
        row[1]: (row[2].upper(), row[3], _default(row[4]), row[5], row[6])
        for row in connection.execute(f"PRAGMA table_xinfo({table})")
    }


def _foreign_keys(connection: sqlite3.Connection, table: str) -> list:
    return sorted(tuple(row[1:]) for row in connection.execute(f"PRAGMA foreign_key_list({table})"))


def _validate_table(
    connection: sqlite3.Connection, expected: sqlite3.Connection, table: str,
    missing_additive: set[str] | frozenset[str] = frozenset(),
) -> None:
    expected_columns = _columns(expected, table)
    for column in missing_additive:
        expected_columns.pop(column)
    if _columns(connection, table) != expected_columns:
        raise RuntimeError(f"Incompatible {table} columns, defaults or primary key")
    if _foreign_keys(connection, table) != _foreign_keys(expected, table):
        raise RuntimeError(f"Incompatible {table} foreign keys")
    sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
    ).fetchone()[0]
    tokens = _tokens(sql)
    if "check" in tokens or "deferrable" in tokens or any(
        tokens[index:index + 2] == ["on", "conflict"] for index in range(len(tokens) - 1)
    ):
        raise RuntimeError(f"Incompatible {table} constraints")
    for row in connection.execute(f"PRAGMA index_list({table})"):
        if row[2] and row[3] != "pk":
            raise RuntimeError(f"Incompatible {table} unique constraint")
        if row[3] == "pk":
            name = row[1].replace('"', '""')
            keys = [tuple(key[2:5]) for key in connection.execute(f'PRAGMA index_xinfo("{name}")') if key[5]]
            primary_key = sorted((value[3], column) for column, value in expected_columns.items() if value[3])
            if keys != [(column, 0, "BINARY") for _, column in primary_key]:
                raise RuntimeError(f"Incompatible {table} primary-key index")


def _validate_email_index(connection: sqlite3.Connection) -> None:
    indexes = {row[1]: row for row in connection.execute("PRAGMA index_list(useraiemailidentity)")}
    index = indexes.get(EMAIL_INDEX)
    keys = [tuple(row[2:5]) for row in connection.execute(f"PRAGMA index_xinfo({EMAIL_INDEX})") if row[5]]
    if index is None or index[2] or index[3] != "c" or index[4] or keys != [("verified_email", 0, "BINARY")]:
        raise RuntimeError("Incompatible verified-email index")


def migrate(database_path: Path) -> bool:
    if not database_path.is_file():
        raise FileNotFoundError("Existing database required")
    uri = database_path.resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        if "user" not in existing:
            raise RuntimeError("Existing user table required")
        user_columns = list(connection.execute("PRAGMA table_info(user)"))
        if [row[1] for row in user_columns if row[5]] != ["id"]:
            raise RuntimeError("Existing user.id primary key required")
        had_index = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?", (EMAIL_INDEX,),
        ).fetchone() is not None
        changed = not set(TABLES).issubset(existing) or not had_index
        # Validate against the migration's canonical DDL without importing settings
        # or application models. Fresh SQLModel tables have the same contract.
        with closing(sqlite3.connect(":memory:")) as expected:
            for table, ddl in TABLES.items():
                kind = connection.execute("SELECT type FROM sqlite_master WHERE name = ?", (table,)).fetchone()
                if kind is not None and kind[0] != "table":
                    raise RuntimeError(f"Existing {table} must be a table")
                expected.execute(ddl)
                connection.execute(ddl)
                if table == "useraiconsent":
                    missing = set(GRANT_COLUMNS).difference(_columns(connection, table))
                    if missing:
                        _validate_table(connection, expected, table, missing)
                        for column, kind in GRANT_COLUMNS.items():
                            if column in missing:
                                connection.execute(f"ALTER TABLE useraiconsent ADD COLUMN {column} {kind}")
                        changed = True
                _validate_table(connection, expected, table)
        connection.execute(f"CREATE INDEX IF NOT EXISTS {EMAIL_INDEX} ON useraiemailidentity (verified_email)")
        _validate_email_index(connection)
        return changed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    changed = migrate(parser.parse_args().database)
    print("AI consent tables added; existing accounts remain denied" if changed else "already migrated")
