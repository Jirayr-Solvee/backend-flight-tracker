#!/usr/bin/env python3
"""Read-only closable-flow test report: per-arm aggregates as JSON.

Opens the SQLite database with `mode=ro` and `query_only`, so it cannot write.
It deliberately does not import the `core` package: its initializer opens the
application database and creates tables. Output contains counts, rates and
native-currency totals only: no installation, user or transaction identifiers.

Example (on the server, from the repository directory):
  venv/bin/python3 scripts/report_closable_flow_test.py --database database.db --revision 1
Optional USD estimate: --usd-rates frozen_fx.json ({"EUR": 1.08, ...}: USD per
unit, frozen on launch day) and --proceeds-ratio (default 0.80).
"""

import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


CORE_DIRECTORY = Path(__file__).resolve().parents[1] / "core"
sys.path.insert(0, str(CORE_DIRECTORY))
import closable_flow_report  # noqa: E402


def connect_read_only(database):
    target = Path(database).resolve(strict=True)
    connection = sqlite3.connect(f"file:{quote(target.as_posix(), safe='/')}?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only = ON")
    connection.row_factory = sqlite3.Row
    return connection


def sqlite_query(connection):
    def query(sql, params=()):
        return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]
    return query


def run(argv=None, *, now_ms=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default="database.db")
    parser.add_argument("--revision", type=int, required=True, help="Measurement revision; revisions are never pooled")
    parser.add_argument("--environment", choices=sorted(closable_flow_report.PURCHASE_ENVIRONMENTS), default="production")
    parser.add_argument("--as-of-ms", type=int)
    parser.add_argument("--since-ms", type=int)
    parser.add_argument("--until-ms", type=int)
    parser.add_argument("--app-version")
    parser.add_argument("--build-number")
    parser.add_argument("--usd-rates", help="Frozen FX table: JSON object of USD per currency unit")
    parser.add_argument("--proceeds-ratio", type=float, default=0.80)
    args = parser.parse_args(argv)
    now = int(time.time() * 1_000) if now_ms is None else now_ms
    try:
        rates = None
        if args.usd_rates:
            rates = json.loads(Path(args.usd_rates).read_text())
            if not isinstance(rates, dict):
                raise ValueError("FX table must be an object")
        with closing(connect_read_only(args.database)) as connection:
            return closable_flow_report.build_summary(
                sqlite_query(connection), analytics_environment=args.environment, revision=args.revision,
                as_of_ms=args.as_of_ms, now=now, since_ms=args.since_ms, until_ms=args.until_ms,
                app_version=args.app_version, build_number=args.build_number,
                usd_rates=rates, proceeds_ratio=args.proceeds_ratio)
    except ValueError:
        return {"error": "invalid_report_window_revision_or_fx_table"}
    except (OSError, sqlite3.Error):
        # Never echo database paths or driver messages.
        return {"error": "database_unavailable_or_schema_missing"}


def main(argv=None):
    result = run(argv)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
