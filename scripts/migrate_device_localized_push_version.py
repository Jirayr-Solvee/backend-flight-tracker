#!/usr/bin/env python3
"""Add the versioned push dictionary capability to an existing SQLite database.

Deployment prerequisite: run this migration before starting code that queries
Device.localized_push_version. Existing rows retain version 1 and the original
supports_localized_push flag. Version 2 is granted only by an explicit app token
refresh, never by this migration. This script does not load app settings.

Usage: python scripts/migrate_device_localized_push_version.py /exact/database.db
"""

import argparse
import re
import sqlite3
from contextlib import closing
from pathlib import Path


VERSION_CHECK = "typeof(localized_push_version) = 'integer' AND localized_push_version BETWEEN 0 AND 2"


def _tokens(sql: str) -> list[str]:
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


def _checks(tokens: list[str]) -> list[list[str]]:
    expressions = []
    for index, token in enumerate(tokens[:-1]):
        if token != "check" or tokens[index + 1] != "(":
            continue
        depth = 1
        end = index + 2
        while end < len(tokens) and depth:
            depth += (tokens[end] == "(") - (tokens[end] == ")")
            end += 1
        if depth:
            raise RuntimeError("Incompatible device CHECK constraint")
        expressions.append(tokens[index + 2:end - 1])
    return expressions


def _validate_version(connection: sqlite3.Connection) -> None:
    columns = {row[1]: row for row in connection.execute("PRAGMA table_xinfo(device)")}
    column = columns.get("localized_push_version")
    if column is None:
        raise RuntimeError("Missing localized_push_version column")
    default = column[4].strip() if column[4] is not None else None
    while default and default.startswith("(") and default.endswith(")"):
        default = default[1:-1].strip()
    if default in {"'1'", '"1"'}:
        default = "1"
    if (column[2].upper(), column[3], default, column[5], column[6]) != ("INTEGER", 1, "1", 0, 0):
        raise RuntimeError("Incompatible localized_push_version type, nullability, default or primary key")
    if any(row[3] == "localized_push_version" for row in connection.execute("PRAGMA foreign_key_list(device)")):
        raise RuntimeError("Incompatible localized_push_version foreign key")
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'device'").fetchone()[0]
    tokens = _tokens(sql)
    expected = _tokens(VERSION_CHECK)
    version_checks = [expression for expression in _checks(tokens) if "localized_push_version" in expression]
    if not version_checks or any(expression != expected for expression in version_checks):
        raise RuntimeError("Incompatible localized_push_version CHECK constraint")
    # An ON CONFLICT override could silently coerce NULL to the default. Locate
    # just this column's declaration so unrelated legacy columns stay untouched.
    start = tokens.index("(") + 1
    depth = 0
    for end in range(start, len(tokens)):
        token = tokens[end]
        if depth == 0 and token in {",", ")"}:
            declaration = tokens[start:end]
            if declaration and declaration[0] == "localized_push_version" and any(
                declaration[index:index + 2] == ["on", "conflict"]
                for index in range(len(declaration) - 1)
            ):
                raise RuntimeError("Incompatible localized_push_version conflict policy")
            start = end + 1
        depth += (token == "(") - (token == ")")
    for row in connection.execute("PRAGMA index_list(device)"):
        if row[2]:
            index_sql = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (row[1],)).fetchone()[0]
            # Include autoindexes (whose sql is NULL) and expression indexes.
            name = row[1].replace('"', '""')
            keys = [key[2] for key in connection.execute(f'PRAGMA index_xinfo("{name}")') if key[5]]
            if "localized_push_version" in keys or "localized_push_version" in _tokens(index_sql or ""):
                raise RuntimeError("Incompatible localized_push_version unique constraint")
    if connection.execute(
        "SELECT 1 FROM device WHERE typeof(localized_push_version) != 'integer' "
        "OR localized_push_version NOT BETWEEN 0 AND 2 LIMIT 1"
    ).fetchone():
        raise RuntimeError("Incompatible existing localized_push_version values")


def migrate(database_path: Path) -> bool:
    if not database_path.is_file():
        raise FileNotFoundError(f"Database not found: {database_path}")

    # mode=rw also prevents accidentally creating a database if it disappears
    # between the existence check and opening the connection.
    uri = database_path.resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        table = connection.execute("SELECT type FROM sqlite_master WHERE name = 'device'").fetchone()
        if table is None or table[0] != "table":
            raise RuntimeError("Existing device table required")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(device)")}
        if "supports_localized_push" not in columns:
            raise RuntimeError("The device table must first have the original localized-push capability")
        if "localized_push_version" in columns:
            _validate_version(connection)
            return False

        connection.execute(
            "ALTER TABLE device ADD COLUMN localized_push_version INTEGER NOT NULL DEFAULT 1 "
            f"CHECK ({VERSION_CHECK})"
        )
        _validate_version(connection)
        return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Exact path to the existing SQLite database")
    changed = migrate(parser.parse_args().database)
    print("localized push dictionary version column added" if changed else "already migrated")
