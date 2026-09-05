import asyncio
import json
import os
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy.pool import StaticPool


TEST_FILE = Path(__file__).resolve()
REPOSITORY_ROOT = TEST_FILE.parents[1]

for key, value in {
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_BUCKET_NAME": "test",
    "AWS_REGION": "eu-north-1",
    "LAMBDA_FUNCTION_AUTH_TOKEN": "test",
    "GEMINI_API_KEY": "test",
    "API_URL": "https://example.invalid",
    "JWT_SECRET": "test",
    "JWT_ALGORITHM": "HS256",
    "JWT_EXPIRE_DAYS": "1",
    "KEY_ID": "test",
    "ISSUER_ID": "test",
    "BUNDLE_ID": "com.zhirayr.Flight-tracker-shared",
    "APP_APPLE_ID": "1",
    "TEAM_ID": "test",
    "X_API_MARKET_KEY": "test",
    "AERODATABOX_SERVICE_URL": "https://example.invalid",
    "BALANCE_REFILL_AMMOUNT": "1",
    "BALANCE_REFILL_THRESHOLD": "1",
    "JWS_ENV": "XCODE",
    "MAX_PREMIUM_HOURS": "1",
    "APPLE_ISSUER": "test",
    "APPLE_KEYS_URL": "https://example.invalid",
    "GUEST_KEY": "test",
    "APN_KEY_PATH": str(TEST_FILE),
    "APPLE_ROOT_CERT_PATH": str(TEST_FILE),
    "AIRLINE_MAP_JSON": str(REPOSITORY_ROOT / "iata_to_icao.json"),
}.items():
    os.environ.setdefault(key, value)


from sqlmodel import Session, SQLModel, create_engine, select

from core.models.apple_ads import (
    AppleAdsAttribution,
    AppleAdsSpendDaily,
    AppStoreRevenueEvent,
)
from core.models.device import Device
from core.models.subscription import Subscription
from core.models.transaction import Transaction
from core.models.user import User
from core.routers.apple_ads import (
    AttributionRequest,
    backfill_revenue,
    record_attribution,
)
from core.services.apple_ads import AppleAdsClient, parse_report_rows
from core.services.apple_ads_reporting import build_measurement_report
from core.services.exchange_rates import fetch_latest_ecb_rates


def utc_ms(year: int, month: int, day: int) -> int:
    return int(
        datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1_000
    )


class AppleAdsMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(id="11111111-2222-4333-8444-555555555555")
        self.device = Device(id="install-1", user_id=self.user.id)
        self.session.add(self.user)
        self.session.add(self.device)
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_daily_report_parser_preserves_money_and_keyword_dimensions(self):
        payload = {
            "data": {
                "reportingDataResponse": {
                    "row": [
                        {
                            "metadata": {
                                "orgId": 10,
                                "campaignId": 20,
                                "campaignName": "Brand",
                                "adGroupId": 30,
                                "adGroupName": "Exact",
                                "keywordId": 40,
                                "keyword": "flight tracker",
                                "matchType": "EXACT",
                            },
                            "granularity": [
                                {
                                    "date": "2026-08-19",
                                    "localSpend": {
                                        "amount": "12.345678",
                                        "currency": "USD",
                                    },
                                    "impressions": 100,
                                    "taps": 20,
                                    "tapInstalls": 7,
                                    "totalInstalls": 8,
                                }
                            ],
                        }
                    ]
                }
            }
        }

        rows = parse_report_rows(
            response_payload=payload,
            dimension_level="keyword",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].spend_microunits, 12_345_678)
        self.assertEqual(rows[0].keyword_id, 40)
        self.assertEqual(rows[0].tap_installs, 7)

    def test_reporting_requests_include_required_dimension_ordering(self):
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"data": {"reportingDataResponse": {"row": []}}},
                request=request,
            )

        async def fetch():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as http_client:
                client = AppleAdsClient(http_client)
                client._api_headers = AsyncMock(
                    return_value={"Authorization": "Bearer test"}
                )
                for dimension_level in ("ad_group", "keyword"):
                    await client._report_pages(
                        campaign_id=20,
                        dimension_level=dimension_level,
                        start_date=date(2026, 8, 1),
                        end_date=date(2026, 8, 2),
                    )

        asyncio.run(fetch())

        self.assertEqual(
            requests[0]["selector"]["orderBy"],
            [{"field": "adGroupId", "sortOrder": "ASCENDING"}],
        )
        self.assertEqual(
            requests[1]["selector"]["orderBy"],
            [{"field": "keywordId", "sortOrder": "ASCENDING"}],
        )

    def test_adservices_result_is_idempotent_and_token_is_not_persisted(self):
        request = AttributionRequest(
            attribution_token="short-lived-secret-token",
            device_id=self.device.id,
            app_version="3.4",
            build_number="109",
            analytics_environment="production",
        )
        apple_payload = {
            "attribution": True,
            "orgId": 10,
            "campaignId": 20,
            "adGroupId": 30,
            "keywordId": 40,
            "countryOrRegion": "US",
        }
        exchange = AsyncMock(return_value=apple_payload)
        with patch(
            "core.routers.apple_ads.AppleAdsClient.exchange_attribution_token",
            exchange,
        ):
            first = asyncio.run(
                record_attribution(request, self.user, self.session)
            )
            second = asyncio.run(
                record_attribution(request, self.user, self.session)
            )

        self.assertEqual(first, {"detail": "recorded", "attributed": True})
        self.assertEqual(second, {"detail": "already_recorded", "attributed": True})
        self.assertEqual(exchange.await_count, 1)
        stored = self.session.get(AppleAdsAttribution, self.device.id)
        self.assertEqual(stored.campaign_id, 20)
        self.assertFalse(hasattr(stored, "attribution_token"))

    def test_ecb_csv_rates_are_parsed_without_third_party_fx_data(self):
        async def handler(request):
            self.assertEqual(request.url.host, "data-api.ecb.europa.eu")
            return httpx.Response(
                200,
                text=(
                    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,"
                    "TIME_PERIOD,OBS_VALUE\n"
                    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-08-18,1.20\n"
                    "EXR.D.GBP.EUR.SP00.A,D,GBP,EUR,SP00,A,2026-08-18,0.86\n"
                ),
                request=request,
            )

        async def fetch():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                return await fetch_latest_ecb_rates(
                    {"USD", "GBP", "EUR"}, client=client
                )

        self.assertEqual(
            asyncio.run(fetch()),
            {"EUR": 1.0, "USD": 1.2, "GBP": 0.86},
        )

    def test_historic_verified_transactions_can_be_backfilled(self):
        subscription = Subscription(id="original-backfill")
        self.user.subscriptions.append(subscription)
        transaction = Transaction(
            id="historic-transaction",
            subscription_id=subscription.id,
            product_id="yearly",
            purchase_date=utc_ms(2026, 7, 1),
            environment="Production",
            transaction_reason="PURCHASE",
            price=19_990,
            currency="USD",
        )
        self.session.add(subscription)
        self.session.add(transaction)
        self.session.commit()

        result = backfill_revenue(self.session)

        self.assertEqual(result["created"], 1)
        event = self.session.get(AppStoreRevenueEvent, transaction.id)
        self.assertEqual(event.user_id, self.user.id)
        self.assertEqual(event.price_milliunits, 19_990)
        self.assertFalse(event.starts_trial)

    def test_partial_refund_millipercent_boundaries_in_ads_report(self):
        self.session.add(AppleAdsAttribution(
            id=self.device.id, user_id=self.user.id, attributed=True, org_id=10,
            campaign_id=20, analytics_environment="production",
            app_version="3.7", build_number="117",
            first_reported_at_ms=utc_ms(2026, 8, 1),
        ))
        paid = AppStoreRevenueEvent(
            id="partial-paid", original_transaction_id="original-1", user_id=self.user.id,
            product_id="yearly", purchase_date_ms=utc_ms(2026, 8, 2),
            purchase_environment="Production", price_milliunits=40_000, currency="USD",
            revoked_date_ms=utc_ms(2026, 8, 3),
        )
        for percentage, net in ((50_000, 20), (0, 40), (100_000, 0), (None, 0)):
            with self.subTest(percentage=percentage):
                paid.revocation_percentage = percentage
                self.session.add(paid)
                self.session.commit()
                row = build_measurement_report(
                    session=self.session, start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 1), dimension="campaign",
                )["rows"][0]
                self.assertEqual(row["gross_revenue"], {"USD": 40})
                self.assertEqual(row["net_revenue"], {"USD": net})

    def test_report_joins_spend_install_trial_and_paid_revenue(self):
        attribution = AppleAdsAttribution(
            id=self.device.id,
            user_id=self.user.id,
            attributed=True,
            org_id=10,
            campaign_id=20,
            ad_group_id=30,
            keyword_id=None,
            app_version="3.4",
            build_number="109",
            analytics_environment="production",
            first_reported_at_ms=utc_ms(2026, 8, 1),
        )
        ad_group = AppleAdsSpendDaily(
            id="ad-group",
            date="2026-08-01",
            dimension_level="ad_group",
            org_id=10,
            campaign_id=20,
            campaign_name="Brand",
            ad_group_id=30,
            ad_group_name="Discovery",
            currency="USD",
            spend_microunits=10_000_000,
            impressions=100,
            taps=20,
            total_installs=5,
        )
        explicit_keyword = AppleAdsSpendDaily(
            id="keyword",
            date="2026-08-01",
            dimension_level="keyword",
            org_id=10,
            campaign_id=20,
            campaign_name="Brand",
            ad_group_id=30,
            ad_group_name="Discovery",
            keyword_id=40,
            keyword="flight tracker",
            match_type="EXACT",
            currency="USD",
            spend_microunits=6_000_000,
            impressions=60,
            taps=12,
            total_installs=3,
        )
        trial = AppStoreRevenueEvent(
            id="transaction-trial",
            original_transaction_id="original-1",
            user_id=self.user.id,
            product_id="yearly-trial",
            purchase_date_ms=utc_ms(2026, 8, 1) + 3_600_000,
            purchase_environment="Production",
            price_milliunits=0,
            currency="USD",
            offer_discount_type="FREE_TRIAL",
            starts_trial=True,
        )
        renewal = AppStoreRevenueEvent(
            id="transaction-renewal",
            original_transaction_id="original-1",
            user_id=self.user.id,
            product_id="yearly-trial",
            purchase_date_ms=utc_ms(2026, 8, 3),
            purchase_environment="Production",
            price_milliunits=9_990,
            currency="USD",
            transaction_reason="RENEWAL",
            starts_trial=False,
        )
        self.session.add(attribution)
        self.session.add(ad_group)
        self.session.add(explicit_keyword)
        self.session.add(trial)
        self.session.add(renewal)
        self.session.commit()

        campaign_report = build_measurement_report(
            session=self.session,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            dimension="campaign",
        )
        row = campaign_report["rows"][0]
        self.assertEqual(row["spend"], {"USD": 10.0})
        self.assertEqual(row["attributed_installs"], 1)
        self.assertEqual(row["verified_trial_users"], 1)
        self.assertEqual(row["paying_customers"], 1)
        self.assertEqual(row["cac"], {"amount": 10.0, "currency": "USD"})
        self.assertEqual(row["net_revenue"], {"USD": 9.99})
        self.assertEqual(row["roas_d7"], 0.999)

        keyword_report = build_measurement_report(
            session=self.session,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            dimension="keyword",
        )
        search_match = next(
            item for item in keyword_report["rows"] if item["keyword_id"] is None
        )
        self.assertEqual(search_match["spend"], {"USD": 4.0})
        self.assertEqual(search_match["keyword"], "Search Match")
        self.assertEqual(search_match["attributed_installs"], 1)


if __name__ == "__main__":
    unittest.main()
