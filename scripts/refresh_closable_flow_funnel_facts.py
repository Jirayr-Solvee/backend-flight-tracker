#!/usr/bin/env python3
"""Daily job: derive the non-purged closable-flow funnel ledger.

Run once a day on the server from the repository directory, before the 90-day
diagnostics purge can reach any enrollment still inside its 35-day horizon:
  venv/bin/python3 -m scripts.refresh_closable_flow_funnel_facts
Prints aggregate counts only. `--dry-run` derives everything and rolls back.
"""

import argparse
import json

from sqlmodel import Session

from core.models import engine
from core.services.closable_flow_funnel import refresh_closable_flow_funnel_facts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    with Session(engine) as session:
        try:
            result = refresh_closable_flow_funnel_facts(session=session)
            if args.dry_run:
                session.rollback()
            else:
                session.commit()
        except Exception:
            session.rollback()
            print(json.dumps({"outcome": "failed"}))
            return 1
    print(json.dumps({"outcome": "dry_run" if args.dry_run else "success", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
