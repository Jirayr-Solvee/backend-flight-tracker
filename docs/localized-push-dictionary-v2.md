# Localized push dictionary version 2

The APNs token refresh request accepts `localized_push_version` as a strict JSON
integer from 0 through 2. Omission means version 1. Booleans, strings, fractions,
null, negative values and unsupported versions are rejected with HTTP 422 before
the handler changes device state. The existing `supports_localized_push` flag
remains required for localized delivery.

| Device capability | Existing flight update alerts | Forwarded-email flight-added alert |
| --- | --- | --- |
| Support flag false, or version 0 | English fallback | English fallback |
| Support flag true, version 1 or omitted | Existing dictionary keys | English fallback |
| Support flag true, version 2 | Existing dictionary keys | Version 2 keys |

Version 2 adds these app-bundle localization entries:

- `New flight added to your account`: no arguments.
- `Flight %@ has been added to your account automatically from your forwarded email.`:
  one argument, the flight number.

An app must ship both keys in its supported localization dictionaries before
advertising version 2. New keys are not inferred from the old boolean flag.
Refreshing from an older app without the version field downgrades that device to
version 1. The migration never upgrades a device to version 2 automatically.

Missing, empty or whitespace-only aircraft model names use the existing
`Aircraft information has been updated for flight %@.` key with the flight
number. This avoids an untranslated English model-name argument. Legacy English
text and notification identity, routing and update fields are unchanged.

## Database prerequisite

Existing databases need `device.localized_push_version` before this application
revision starts querying devices. `SQLModel.metadata.create_all` does not add a
column to an existing table. The original `supports_localized_push` migration
must already have been applied.

Under a separately authorized deployment, back up the exact target database and
coordinate the schema step with the application's existing restart/readiness
procedure. Run with the target environment's Python interpreter:

```sh
python scripts/migrate_device_localized_push_version.py /exact/path/to/database.db
```

The migration requires an explicit existing SQLite database, performs its change
in one transaction, preserves the original support flags, and gives existing rows
version 1. It is idempotent and does not import application settings. Do not
restart the new application revision if the migration fails.

This change does not deploy services or run the migration against an application
database. The automated tests exercise it only on disposable synthetic databases.
