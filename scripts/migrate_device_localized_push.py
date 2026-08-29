#!/usr/bin/env python3
"""Add the localized-push capability flag to an existing SQLite database."""

import sqlite3
import sys
from pathlib import Path


DATABASE_PATH = Path(__file__).resolve().parents[1] / "database.db"
COLUMN_NAME = "supports_localized_push"


def migrate(database_path: Path = DATABASE_PATH) -> bool:
    if not database_path.exists():
        raise FileNotFoundError(f"Database not found: {database_path}")

    with sqlite3.connect(database_path, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(device)")
        }
        if COLUMN_NAME in columns:
            return False

        connection.execute(
            "ALTER TABLE device "
            "ADD COLUMN supports_localized_push BOOLEAN NOT NULL DEFAULT 0"
        )
        connection.commit()
        return True


if __name__ == "__main__":
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DATABASE_PATH
    changed = migrate(target)
    print("localized push capability column added" if changed else "already migrated")
