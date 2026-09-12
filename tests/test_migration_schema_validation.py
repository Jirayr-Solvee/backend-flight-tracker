"""Credential-free SQLite migration contracts, including rejected-schema rollback.

Run directly or with unittest discovery. Only the two standalone migration
modules are loaded; these tests never import core, settings, SQLModel, or the
application's database engine. Every database is a disposable synthetic fixture.
"""

import importlib.util
import itertools
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[1]


def load_migration(name):
    specification = importlib.util.spec_from_file_location(
        name, REPOSITORY / "scripts" / f"{name}.py"
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("Standalone migration module is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.migrate


migrate_consent = load_migration("migrate_ai_consent")
migrate_device = load_migration("migrate_device_localized_push_version")


CONSENT_TABLES = {
    "useraiconsent": """
        CREATE TABLE useraiconsent (
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
        CREATE TABLE aiconsentreceipt (
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
        CREATE TABLE useraiemailidentity (
            user_id VARCHAR NOT NULL PRIMARY KEY REFERENCES user(id),
            apple_id VARCHAR NOT NULL,
            verified_email VARCHAR NOT NULL
        )
    """,
    "useraiemailreceipt": """
        CREATE TABLE useraiemailreceipt (
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

CONSENT_ROWS = {
    "useraiconsent": {
        "user_id": "synthetic-owner", "policy_version": 1, "revision": 3,
        "search_enabled": 1, "forwarded_email_enabled": 1, "updated_at": 123456,
        "forwarded_email_grant_id": "synthetic-grant",
        "forwarded_email_granted_at_ms": 123400,
    },
    "aiconsentreceipt": {
        "user_id": "synthetic-owner", "request_id": "synthetic-request",
        "policy_version": 1, "expected_revision": 2, "purpose": "search", "enabled": 1,
    },
    "useraiemailidentity": {
        "user_id": "synthetic-owner", "apple_id": "synthetic-apple-subject",
        "verified_email": "synthetic@example.invalid",
    },
    "useraiemailreceipt": {
        "receipt_digest": "synthetic-receipt-digest", "proof_digest": "synthetic-proof-digest",
        "owner_binding": "synthetic-owner-binding", "received_at_ms": 123410,
        "claimed_at_ms": 123420, "state": "completed", "finished_at_ms": 123430,
    },
}

EMAIL_INDEX = (
    "CREATE INDEX ix_useraiemailidentity_verified_email "
    "ON useraiemailidentity (verified_email)"
)
VERSION_CHECK = (
    "CHECK (typeof(localized_push_version) = 'integer' "
    "AND localized_push_version BETWEEN 0 AND 2)"
)
VERSION_COLUMN = "localized_push_version INTEGER NOT NULL DEFAULT 1 " + VERSION_CHECK


class MigrationSchemaValidationTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="sofly-migration-schema-")
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)
        self.sequence = itertools.count()

    def database_path(self):
        return self.directory / f"fixture-{next(self.sequence)}.db"

    def legacy_consent_definitions(self):
        definitions = {table: sql for table, sql in CONSENT_TABLES.items() if table != "useraiemailreceipt"}
        definitions["useraiconsent"] = definitions["useraiconsent"].replace(
            "            forwarded_email_grant_id VARCHAR,\n", ""
        ).replace("            forwarded_email_granted_at_ms INTEGER,\n", "")
        return definitions

    def create_consent_database(self, tables=None, *, index=True, seed=True):
        database = self.database_path()
        definitions = CONSENT_TABLES if tables is None else tables
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE user (id VARCHAR NOT NULL PRIMARY KEY, email VARCHAR)")
            connection.execute(
                "INSERT INTO user VALUES (?, ?)",
                ("synthetic-owner", "legacy@example.invalid"),
            )
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('preserve unrelated data')")
            for table, definition in definitions.items():
                connection.execute(definition)
                if seed:
                    present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    values = {key: value for key, value in CONSENT_ROWS[table].items() if key in present}
                    connection.execute(
                        f"INSERT INTO {table} ({', '.join(values)}) "
                        f"VALUES ({', '.join('?' for _ in values)})",
                        tuple(values.values()),
                    )
            if index:
                connection.execute(EMAIL_INDEX)
        return database

    def create_device_database(self, *, version=None, supports="BOOLEAN NOT NULL DEFAULT 0", seed=True):
        database = self.database_path()
        with closing(sqlite3.connect(database)) as connection, connection:
            version_definition = f", {version}" if version else ""
            # A malformed version PK must still be valid SQLite DDL, so remove
            # the otherwise canonical id PK in that dedicated negative fixture.
            id_definition = "VARCHAR NOT NULL" if version and "PRIMARY KEY" in version else "VARCHAR NOT NULL PRIMARY KEY"
            connection.execute(
                f"CREATE TABLE device (id {id_definition}, "
                f"supports_localized_push {supports}, apn_token VARCHAR{version_definition})"
            )
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('preserve unrelated data')")
            if seed:
                if version:
                    connection.execute(
                        "INSERT INTO device (id, supports_localized_push, apn_token, localized_push_version) "
                        "VALUES (?, ?, ?, ?)",
                        ("synthetic-device", 1, "synthetic-token", 1),
                    )
                else:
                    connection.executemany(
                        "INSERT INTO device (id, supports_localized_push, apn_token) VALUES (?, ?, ?)",
                        [("synthetic-off", 0, "off-token"), ("synthetic-on", 1, "on-token")],
                    )
        return database

    def snapshot(self, database):
        with closing(sqlite3.connect(database)) as connection:
            # sqlite3.iterdump() on newer Python runs foreign_key_check, which
            # cannot inspect intentionally malformed FK fixtures. Read the
            # logical schema and every table's rows directly instead.
            schema = tuple(connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ))
            rows = []
            for kind, name, _, _ in schema:
                if kind == "table":
                    identifier = '"' + name.replace('"', '""') + '"'
                    data = tuple(sorted(connection.execute(f"SELECT * FROM {identifier}"), key=repr))
                    rows.append((name, data))
            return (
                schema, tuple(rows),
                connection.execute("PRAGMA schema_version").fetchone()[0],
            )

    def assert_rejected_unchanged(self, migrate, database):
        before = self.snapshot(database)
        with self.assertRaises(RuntimeError):
            migrate(database)
        self.assertEqual(self.snapshot(database), before, "Rejected migration changed schema or data")

    def test_consent_creation_is_default_deny_and_idempotent(self):
        database = self.create_consent_database({}, index=False)
        with closing(sqlite3.connect(database)) as connection:
            previous_users = connection.execute("SELECT * FROM user").fetchall()
        self.assertTrue(migrate_consent(database))
        with closing(sqlite3.connect(database)) as connection, connection:
            self.assertEqual(connection.execute("SELECT * FROM user").fetchall(), previous_users)
            for table in CONSENT_TABLES:
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            connection.execute("INSERT INTO useraiconsent (user_id) VALUES ('synthetic-owner')")
            self.assertEqual(connection.execute(
                "SELECT policy_version, revision, search_enabled, forwarded_email_enabled, updated_at, "
                "forwarded_email_grant_id, forwarded_email_granted_at_ms "
                "FROM useraiconsent"
            ).fetchone(), (1, 0, 0, 0, None, None, None))
            connection.execute(
                "INSERT INTO useraiemailreceipt "
                "(receipt_digest, proof_digest, owner_binding, received_at_ms, claimed_at_ms) "
                "VALUES ('new-receipt', 'new-proof', 'new-binding', 1, 2)"
            )
            self.assertEqual(connection.execute(
                "SELECT state, finished_at_ms FROM useraiemailreceipt"
            ).fetchone(), ("processing", None))
        before = self.snapshot(database)
        self.assertFalse(migrate_consent(database))
        self.assertEqual(self.snapshot(database), before)

    def test_consent_canonical_existing_schema_preserves_grants_and_receipts(self):
        database = self.create_consent_database()
        before = self.snapshot(database)
        self.assertFalse(migrate_consent(database))
        self.assertEqual(self.snapshot(database), before)

    def test_legacy_consent_upgrade_preserves_allow_without_inventing_email_grant(self):
        definitions = self.legacy_consent_definitions()
        database = self.create_consent_database(definitions)
        with closing(sqlite3.connect(database)) as connection:
            previous_rows = {
                table: connection.execute(f"SELECT * FROM {table}").fetchall()
                for table in ("user", "unrelated", *definitions)
            }
            previous_columns = {
                table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
                for table in previous_rows
            }
        self.assertTrue(migrate_consent(database))
        with closing(sqlite3.connect(database)) as connection:
            for table, rows in previous_rows.items():
                columns = ", ".join(previous_columns[table])
                self.assertEqual(connection.execute(f"SELECT {columns} FROM {table}").fetchall(), rows)
            self.assertEqual(connection.execute(
                "SELECT forwarded_email_enabled, forwarded_email_grant_id, forwarded_email_granted_at_ms "
                "FROM useraiconsent"
            ).fetchone(), (1, None, None))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM useraiemailreceipt").fetchone(), (0,))
        before = self.snapshot(database)
        self.assertFalse(migrate_consent(database))
        self.assertEqual(self.snapshot(database), before)

    def test_consent_accepts_sqlmodel_quoted_integer_defaults(self):
        definitions = {
            table: sql.replace("DEFAULT 1", "DEFAULT '1'").replace("DEFAULT 0", "DEFAULT '0'")
            for table, sql in CONSENT_TABLES.items()
        }
        database = self.create_consent_database(definitions)
        before = self.snapshot(database)
        self.assertFalse(migrate_consent(database))
        self.assertEqual(self.snapshot(database), before)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("INSERT INTO user (id) VALUES ('second-synthetic-owner')")
            connection.execute("INSERT INTO useraiconsent (user_id) VALUES ('second-synthetic-owner')")
            self.assertEqual(connection.execute(
                "SELECT policy_version, revision, search_enabled, forwarded_email_enabled "
                "FROM useraiconsent WHERE user_id = 'second-synthetic-owner'"
            ).fetchone(), (1, 0, 0, 0))

    def test_consent_missing_columns_are_rejected_with_ddl_rollback(self):
        cases = (
            ("useraiconsent", "            revision INTEGER NOT NULL DEFAULT 0,\n"),
            ("aiconsentreceipt", "            expected_revision INTEGER NOT NULL,\n"),
            ("useraiemailidentity", "            apple_id VARCHAR NOT NULL,\n"),
            ("useraiemailreceipt", "            proof_digest VARCHAR NOT NULL,\n"),
        )
        for table, removed in cases:
            with self.subTest(table=table):
                self.assertIn(removed, CONSENT_TABLES[table])
                # Other consent tables are absent: validation may occur after
                # tentative additive DDL, but none of it may survive failure.
                database = self.create_consent_database(
                    {table: CONSENT_TABLES[table].replace(removed, "")}, index=False,
                )
                self.assert_rejected_unchanged(migrate_consent, database)

    def test_consent_wrong_types_are_rejected(self):
        cases = (
            ("useraiconsent", "revision INTEGER", "revision TEXT"),
            ("useraiconsent", "search_enabled BOOLEAN", "search_enabled TEXT"),
            ("useraiconsent", "updated_at INTEGER", "updated_at TEXT"),
            ("useraiconsent", "forwarded_email_grant_id VARCHAR", "forwarded_email_grant_id INTEGER"),
            ("useraiconsent", "forwarded_email_granted_at_ms INTEGER", "forwarded_email_granted_at_ms TEXT"),
            ("aiconsentreceipt", "enabled BOOLEAN", "enabled TEXT"),
            ("aiconsentreceipt", "purpose VARCHAR", "purpose INTEGER"),
            ("useraiemailidentity", "verified_email VARCHAR", "verified_email INTEGER"),
            ("useraiemailreceipt", "receipt_digest VARCHAR", "receipt_digest INTEGER"),
            ("useraiemailreceipt", "received_at_ms INTEGER", "received_at_ms TEXT"),
        )
        for table, previous, changed in cases:
            with self.subTest(table=table, column=previous):
                database = self.create_consent_database(
                    {table: CONSENT_TABLES[table].replace(previous, changed)}, index=False,
                    # INTEGER PRIMARY KEY becomes a SQLite rowid alias and
                    # cannot store the synthetic string receipt digest.
                    seed=previous != "receipt_digest VARCHAR",
                )
                self.assert_rejected_unchanged(migrate_consent, database)

    def test_consent_incompatible_nullability_is_rejected(self):
        cases = (
            ("useraiconsent", "policy_version INTEGER NOT NULL", "policy_version INTEGER"),
            ("useraiconsent", "search_enabled BOOLEAN NOT NULL", "search_enabled BOOLEAN"),
            ("useraiconsent", "updated_at INTEGER", "updated_at INTEGER NOT NULL"),
            ("useraiconsent", "forwarded_email_grant_id VARCHAR", "forwarded_email_grant_id VARCHAR NOT NULL"),
            ("useraiconsent", "forwarded_email_granted_at_ms INTEGER", "forwarded_email_granted_at_ms INTEGER NOT NULL"),
            ("aiconsentreceipt", "request_id VARCHAR NOT NULL", "request_id VARCHAR"),
            ("aiconsentreceipt", "enabled BOOLEAN NOT NULL", "enabled BOOLEAN"),
            ("useraiemailidentity", "verified_email VARCHAR NOT NULL", "verified_email VARCHAR"),
            ("useraiemailreceipt", "owner_binding VARCHAR NOT NULL", "owner_binding VARCHAR"),
            ("useraiemailreceipt", "finished_at_ms INTEGER", "finished_at_ms INTEGER NOT NULL"),
        )
        for table, previous, changed in cases:
            with self.subTest(table=table, column=previous):
                database = self.create_consent_database(
                    {table: CONSENT_TABLES[table].replace(previous, changed)}, index=False,
                )
                self.assert_rejected_unchanged(migrate_consent, database)

    def test_consent_missing_or_wrong_defaults_are_rejected(self):
        for column, expected in (
            ("policy_version", 1), ("revision", 0),
            ("search_enabled", 0), ("forwarded_email_enabled", 0),
        ):
            kind = "BOOLEAN" if column.endswith("_enabled") else "INTEGER"
            original = f"{column} {kind} NOT NULL DEFAULT {expected}"
            for replacement in (f"{column} {kind} NOT NULL", f"{column} {kind} NOT NULL DEFAULT {1 - expected}"):
                with self.subTest(column=column, definition=replacement):
                    database = self.create_consent_database({
                        "useraiconsent": CONSENT_TABLES["useraiconsent"].replace(original, replacement)
                    }, index=False)
                    self.assert_rejected_unchanged(migrate_consent, database)

    def test_consent_nullable_fields_and_receipt_state_defaults_are_validated(self):
        cases = (
            ("useraiconsent", "forwarded_email_grant_id VARCHAR", "forwarded_email_grant_id VARCHAR DEFAULT ''"),
            ("useraiconsent", "forwarded_email_granted_at_ms INTEGER", "forwarded_email_granted_at_ms INTEGER DEFAULT 0"),
            ("useraiemailreceipt", "state VARCHAR NOT NULL DEFAULT 'processing'", "state VARCHAR NOT NULL"),
            ("useraiemailreceipt", "DEFAULT 'processing'", "DEFAULT 'completed'"),
            ("useraiemailreceipt", "finished_at_ms INTEGER", "finished_at_ms INTEGER DEFAULT 0"),
        )
        for table, previous, changed in cases:
            with self.subTest(table=table, column=previous):
                database = self.create_consent_database(
                    {table: CONSENT_TABLES[table].replace(previous, changed)}, index=False,
                )
                self.assert_rejected_unchanged(migrate_consent, database)

    def test_consent_wrong_primary_keys_are_rejected(self):
        cases = (
            ("useraiconsent", "NOT NULL PRIMARY KEY REFERENCES", "NOT NULL REFERENCES"),
            ("aiconsentreceipt", "PRIMARY KEY (user_id, request_id)", "PRIMARY KEY (request_id)"),
            ("useraiemailidentity", "NOT NULL PRIMARY KEY REFERENCES", "NOT NULL REFERENCES"),
            ("useraiemailreceipt", "receipt_digest VARCHAR NOT NULL PRIMARY KEY", "receipt_digest VARCHAR NOT NULL"),
        )
        for table, previous, changed in cases:
            with self.subTest(table=table):
                database = self.create_consent_database(
                    {table: CONSENT_TABLES[table].replace(previous, changed)}, index=False,
                )
                self.assert_rejected_unchanged(migrate_consent, database)

    def test_consent_missing_or_wrong_user_foreign_keys_are_rejected(self):
        for table in ("useraiconsent", "aiconsentreceipt", "useraiemailidentity"):
            for replacement in ("", "REFERENCES wrong_owner(id)", "REFERENCES user(email)", "REFERENCES user(id) ON DELETE CASCADE"):
                with self.subTest(table=table, foreign_key=replacement):
                    database = self.create_consent_database(
                        {table: CONSENT_TABLES[table].replace("REFERENCES user(id)", replacement)}, index=False,
                    )
                    self.assert_rejected_unchanged(migrate_consent, database)

    def test_consent_additional_constraints_are_rejected(self):
        cases = (
            ("revision INTEGER NOT NULL DEFAULT 0", "revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)"),
            ("search_enabled BOOLEAN NOT NULL", "search_enabled BOOLEAN NOT NULL ON CONFLICT REPLACE"),
            ("user_id VARCHAR NOT NULL PRIMARY KEY", "user_id VARCHAR COLLATE NOCASE NOT NULL PRIMARY KEY"),
        )
        for previous, changed in cases:
            with self.subTest(constraint=changed):
                database = self.create_consent_database(
                    {"useraiconsent": CONSENT_TABLES["useraiconsent"].replace(previous, changed)}, index=False,
                )
                self.assert_rejected_unchanged(migrate_consent, database)

    def test_bad_email_receipt_rolls_back_legacy_grant_column_additions(self):
        cases = (
            ("received_at_ms INTEGER", "received_at_ms TEXT"),
            ("DEFAULT 'processing'", "DEFAULT 'completed'"),
            ("receipt_digest VARCHAR NOT NULL PRIMARY KEY", "receipt_digest VARCHAR NOT NULL"),
            ("owner_binding VARCHAR NOT NULL", "owner_binding VARCHAR NOT NULL REFERENCES user(id)"),
            ("finished_at_ms INTEGER", "finished_at_ms INTEGER, user_id VARCHAR"),
            ("finished_at_ms INTEGER", "finished_at_ms INTEGER, grant_id VARCHAR"),
        )
        for previous, changed in cases:
            with self.subTest(receipt=changed):
                definitions = self.legacy_consent_definitions()
                definitions["useraiemailreceipt"] = CONSENT_TABLES["useraiemailreceipt"].replace(previous, changed)
                database = self.create_consent_database(definitions, index=False)
                self.assert_rejected_unchanged(migrate_consent, database)

    def test_wrong_named_identity_indexes_are_rejected_without_replacement(self):
        cases = {
            "wrong_table": "CREATE INDEX ix_useraiemailidentity_verified_email ON unrelated(payload)",
            "wrong_column": "CREATE INDEX ix_useraiemailidentity_verified_email ON useraiemailidentity(apple_id)",
            "extra_column": "CREATE INDEX ix_useraiemailidentity_verified_email ON useraiemailidentity(verified_email, user_id)",
            "unique": "CREATE UNIQUE INDEX ix_useraiemailidentity_verified_email ON useraiemailidentity(verified_email)",
            "partial": "CREATE INDEX ix_useraiemailidentity_verified_email ON useraiemailidentity(verified_email) WHERE verified_email IS NOT NULL",
            "expression": "CREATE INDEX ix_useraiemailidentity_verified_email ON useraiemailidentity(lower(verified_email))",
        }
        for label, definition in cases.items():
            with self.subTest(index=label):
                database = self.create_consent_database(index=False)
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute(definition)
                self.assert_rejected_unchanged(migrate_consent, database)

    def test_missing_normal_identity_index_is_repaired_additively(self):
        database = self.create_consent_database(index=False)
        with closing(sqlite3.connect(database)) as connection:
            previous_rows = {
                table: connection.execute(f"SELECT * FROM {table}").fetchall()
                for table in ("user", "unrelated", *CONSENT_TABLES)
            }
        self.assertTrue(migrate_consent(database))
        with closing(sqlite3.connect(database)) as connection:
            for table, rows in previous_rows.items():
                self.assertEqual(connection.execute(f"SELECT * FROM {table}").fetchall(), rows)
            indexes = connection.execute("PRAGMA index_list(useraiemailidentity)").fetchall()
            index = next(row for row in indexes if row[1] == "ix_useraiemailidentity_verified_email")
            self.assertEqual((index[2], index[4]), (0, 0), "Identity index must be nonunique and nonpartial")
            self.assertEqual(
                [row[2] for row in connection.execute("PRAGMA index_info(ix_useraiemailidentity_verified_email)")],
                ["verified_email"],
            )
        before = self.snapshot(database)
        self.assertFalse(migrate_consent(database))
        self.assertEqual(self.snapshot(database), before)

    def test_device_legacy_addition_preserves_capabilities_and_is_idempotent(self):
        database = self.create_device_database()
        self.assertTrue(migrate_device(database))
        with closing(sqlite3.connect(database)) as connection, connection:
            self.assertEqual(connection.execute(
                "SELECT id, supports_localized_push, apn_token, localized_push_version FROM device ORDER BY id"
            ).fetchall(), [("synthetic-off", 0, "off-token", 1), ("synthetic-on", 1, "on-token", 1)])
            connection.execute("UPDATE device SET localized_push_version = 2 WHERE id = 'synthetic-on'")
            connection.execute("INSERT INTO device (id) VALUES ('new-synthetic-device')")
            self.assertEqual(connection.execute(
                "SELECT localized_push_version FROM device WHERE id = 'new-synthetic-device'"
            ).fetchone(), (1,))
        before = self.snapshot(database)
        self.assertFalse(migrate_device(database))
        self.assertEqual(self.snapshot(database), before)

    def test_device_original_capability_shape_is_not_overconstrained(self):
        for supports in ("BOOLEAN", "INTEGER NOT NULL DEFAULT 1", "TEXT DEFAULT 'legacy'"):
            with self.subTest(supports=supports):
                database = self.create_device_database(supports=supports)
                with closing(sqlite3.connect(database)) as connection:
                    before = connection.execute("SELECT id, supports_localized_push, apn_token FROM device ORDER BY id").fetchall()
                self.assertTrue(migrate_device(database))
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(connection.execute("SELECT id, supports_localized_push, apn_token FROM device ORDER BY id").fetchall(), before)
                self.assertFalse(migrate_device(database))

    def test_device_accepts_canonical_and_sqlmodel_quoted_default(self):
        for version in (VERSION_COLUMN, VERSION_COLUMN.replace("DEFAULT 1", "DEFAULT '1'")):
            with self.subTest(version=version):
                database = self.create_device_database(version=version)
                before = self.snapshot(database)
                self.assertFalse(migrate_device(database))
                self.assertEqual(self.snapshot(database), before)

    def test_device_existing_wrong_version_metadata_is_rejected(self):
        cases = {
            "wrong_type": VERSION_COLUMN.replace(" INTEGER ", " TEXT "),
            "nullable": VERSION_COLUMN.replace(" NOT NULL", ""),
            "missing_default": VERSION_COLUMN.replace(" DEFAULT 1", ""),
            "wrong_default": VERSION_COLUMN.replace(" DEFAULT 1", " DEFAULT 2"),
            "primary_key": VERSION_COLUMN.replace(" NOT NULL", " NOT NULL PRIMARY KEY"),
        }
        for label, definition in cases.items():
            with self.subTest(metadata=label):
                database = self.create_device_database(version=definition, seed=label != "wrong_type")
                self.assert_rejected_unchanged(migrate_device, database)

    def test_device_missing_or_weak_version_checks_are_rejected(self):
        checks = {
            "no_check": "",
            "range_only": "CHECK (localized_push_version BETWEEN 0 AND 2)",
            "type_only": "CHECK (typeof(localized_push_version) = 'integer')",
            "wrong_upper_bound": "CHECK (typeof(localized_push_version) = 'integer' AND localized_push_version BETWEEN 0 AND 3)",
            "or_instead_of_and": "CHECK (typeof(localized_push_version) = 'integer' OR localized_push_version BETWEEN 0 AND 2)",
            "tautology": "CHECK (1)",
        }
        for label, check in checks.items():
            with self.subTest(constraint=label):
                database = self.create_device_database(version="localized_push_version INTEGER NOT NULL DEFAULT 1 " + check)
                self.assert_rejected_unchanged(migrate_device, database)

    def test_device_added_constraint_rejects_invalid_values_and_allows_all_versions(self):
        database = self.create_device_database()
        self.assertTrue(migrate_device(database))
        with closing(sqlite3.connect(database)) as connection, connection:
            for version in (0, 1, 2):
                connection.execute(
                    "UPDATE device SET localized_push_version = ? WHERE id = 'synthetic-on'", (version,)
                )
            for value in (None, -1, 3, 1.5, "invalid"):
                with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE device SET localized_push_version = ? WHERE id = 'synthetic-on'", (value,)
                    )

    def test_device_post_alter_validation_failure_rolls_back_new_column(self):
        database = self.create_device_database()

        def fail_validation(connection):
            columns = {row[1] for row in connection.execute("PRAGMA table_info(device)")}
            self.assertIn("localized_push_version", columns, "Failure must occur after additive DDL")
            raise RuntimeError("Synthetic post-ALTER validation failure")

        with patch.dict(migrate_device.__globals__, {"_validate_version": fail_validation}):
            self.assert_rejected_unchanged(migrate_device, database)


if __name__ == "__main__":
    unittest.main()
