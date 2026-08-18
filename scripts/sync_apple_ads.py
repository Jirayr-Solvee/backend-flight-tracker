#!/usr/bin/env python3
import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone

from sqlmodel import Session

from core.models import engine
from core.services.apple_ads import AppleAdsClient


def parse_args():
    today = datetime.now(timezone.utc).date()
    parser = argparse.ArgumentParser(description="Sync Apple Ads daily spend")
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=today - timedelta(days=29),
    )
    parser.add_argument("--end-date", type=date.fromisoformat, default=today)
    parser.add_argument("--campaign-id", type=int, action="append", dest="campaign_ids")
    return parser.parse_args()


async def run():
    args = parse_args()
    async with AppleAdsClient() as client:
        rows = await client.spend_rows(
            start_date=args.start_date,
            end_date=args.end_date,
            campaign_ids=args.campaign_ids,
        )
    with Session(engine) as session:
        for row in rows:
            session.merge(row)
        session.commit()
    print(
        f"Synced {len(rows)} Apple Ads rows for "
        f"{args.start_date.isoformat()} through {args.end_date.isoformat()}"
    )


if __name__ == "__main__":
    asyncio.run(run())
