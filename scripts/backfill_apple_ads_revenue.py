#!/usr/bin/env python3
from sqlmodel import Session

from core.models import engine
from core.services.revenue_measurement import backfill_verified_revenue_events


if __name__ == "__main__":
    with Session(engine) as session:
        result = backfill_verified_revenue_events(session)
    print(
        "Revenue backfill complete: "
        f"created={result['created']} "
        f"already_present={result['already_present']} "
        f"skipped_without_user={result['skipped_without_user']}"
    )
