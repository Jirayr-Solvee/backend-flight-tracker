import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
    "JWT_SECRET": "test-secret",
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

from core.models.search_failure import SearchFailureSample
from core.models.user import User
from core.routers.flights import (
    get_search_failure_report,
    report_app_search_failure,
    search_flights_from_text_post,
)
from core.models.flight import SearchFailureReportRequest, SearchQueryRequest
from core.services.search_failure import RETENTION_MS, SearchFailureService


class SearchFailureReportingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(id="search-test-user")
        self.session.add(self.user)
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_query_is_redacted_encrypted_and_expires_after_seven_days(self):
        query = "EK 822 17 Aug contact me@example.com +1 415 555 1212"
        sample = SearchFailureService.record(
            session=self.session,
            user_id=self.user.id,
            query=query,
            source="backend",
            query_type="flight_number",
            failure_reason="provider_no_match",
            provider_outcome="no_match",
            normalization_applied=False,
            provider_result_count=0,
            structured_args={
                "airline_iata": "EK",
                "flight_number": "822",
                "departure_date": "2026-08-17",
            },
        )
        self.session.commit()

        self.assertNotIn("EK 822", sample.query_ciphertext)
        self.assertEqual(
            SearchFailureService.decrypt_query(sample.query_ciphertext),
            "EK 822 17 Aug contact [email] [phone]",
        )
        self.assertEqual(sample.airline_iata, "EK")
        self.assertEqual(sample.flight_number, "822")
        self.assertEqual(sample.departure_date, "2026-08-17")
        self.assertLessEqual(
            abs((sample.expires_at_ms - sample.created_at_ms) - RETENTION_MS),
            100,
        )

    def test_app_report_enriches_backend_sample_instead_of_duplicating_it(self):
        sample = SearchFailureService.record(
            session=self.session,
            user_id=self.user.id,
            query="DL915 tomorrow",
            source="backend",
            query_type="flight_number",
            failure_reason="provider_no_match",
            provider_outcome="results",
            normalization_applied=False,
            provider_result_count=2,
        )
        self.session.commit()

        response = report_app_search_failure(
            SearchFailureReportRequest(
                query="DL915 tomorrow",
                failure_sample_id=sample.id,
                source="onboarding_treatment",
                query_type="flight_number",
                failure_reason="landed_only",
                provider_outcome="results",
                provider_result_count=2,
                filtered_result_count=2,
                search_journey_id="11111111-2222-4333-8444-555555555555",
                search_attempt_number=1,
                app_version="3.4",
                build_number="108",
            ),
            self.session,
            self.user,
        )

        self.assertEqual(response["failure_sample_id"], sample.id)
        rows = self.session.exec(select(SearchFailureSample)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source, "backend_and_app")
        self.assertEqual(rows[0].failure_reason, "landed_only")
        self.assertEqual(rows[0].filtered_result_count, 2)
        self.assertEqual(rows[0].app_version, "3.4")

    def test_generic_app_reason_does_not_replace_precise_backend_reason(self):
        sample = SearchFailureService.record(
            session=self.session,
            user_id=self.user.id,
            query="Rev",
            source="backend",
            query_type="natural_language",
            failure_reason="unsupported_or_incomplete_query",
            provider_outcome="not_called",
            normalization_applied=False,
            provider_result_count=0,
        )
        self.session.commit()

        report_app_search_failure(
            SearchFailureReportRequest(
                query="Rev",
                failure_sample_id=sample.id,
                source="regular_search",
                query_type="natural_language",
                failure_reason="provider_no_match",
                provider_outcome="not_called",
                app_version="3.4",
                build_number="108",
            ),
            self.session,
            self.user,
        )

        enriched = self.session.get(SearchFailureSample, sample.id)
        self.assertEqual(
            enriched.failure_reason,
            "unsupported_or_incomplete_query",
        )

    def test_backend_preflight_failure_returns_correlatable_sample_id(self):
        response = asyncio.run(
            search_flights_from_text_post(
                payload=SearchQueryRequest(
                    term="Mexico to Turkey",
                    language="en",
                    app_version="3.4",
                    build_number="108",
                    analytics_environment="production",
                ),
                accept_language="en",
                session=self.session,
                user=self.user,
            )
        )

        self.assertEqual(response.diagnostics.failure_reason, "ambiguous_location")
        self.assertIsNotNone(response.diagnostics.failure_sample_id)
        sample = self.session.get(
            SearchFailureSample,
            response.diagnostics.failure_sample_id,
        )
        self.assertIsNotNone(sample)
        self.assertEqual(sample.app_version, "3.4")
        self.assertEqual(sample.build_number, "108")
        self.assertEqual(sample.analytics_environment, "production")

    @patch(
        "core.routers.flights.GeminiService.get_function_call",
        new_callable=AsyncMock,
    )
    def test_unparsed_query_is_recorded_as_not_called_not_provider_no_match(
        self,
        get_function_call: AsyncMock,
    ):
        get_function_call.return_value = None

        response = asyncio.run(
            search_flights_from_text_post(
                payload=SearchQueryRequest(
                    term="Rev",
                    language="en",
                    app_version="3.4",
                    build_number="108",
                    analytics_environment="production",
                ),
                accept_language="en",
                session=self.session,
                user=self.user,
            )
        )

        self.assertEqual(
            response.diagnostics.failure_reason,
            "unsupported_or_incomplete_query",
        )
        self.assertEqual(response.diagnostics.provider_outcome, "not_called")

    def test_report_hides_query_by_default_and_can_return_redacted_sample(self):
        SearchFailureService.record(
            session=self.session,
            user_id=self.user.id,
            query="V7 2115 17 August",
            source="backend",
            query_type="flight_number",
            failure_reason="provider_no_match",
            provider_outcome="no_match",
            normalization_applied=True,
            provider_result_count=0,
        )
        self.session.commit()

        hidden = get_search_failure_report(
            days=7,
            limit=100,
            include_samples=False,
            analytics_environment=None,
            session=self.session,
        )
        visible = get_search_failure_report(
            days=7,
            limit=100,
            include_samples=True,
            analytics_environment=None,
            session=self.session,
        )
        self.assertNotIn("redacted_query", hidden["recent_samples"][0])
        self.assertEqual(
            visible["recent_samples"][0]["redacted_query"],
            "V7 2115 17 August",
        )

    def test_expired_samples_are_deleted(self):
        sample = SearchFailureService.record(
            session=self.session,
            user_id=self.user.id,
            query="old query",
            source="backend",
            query_type="unknown",
            failure_reason="provider_no_match",
            provider_outcome="no_match",
            normalization_applied=False,
            provider_result_count=0,
        )
        sample.expires_at_ms = 1
        self.session.add(sample)
        self.session.commit()

        SearchFailureService.purge_expired(self.session, now_ms=2)
        self.session.commit()
        self.assertEqual(self.session.exec(select(SearchFailureSample)).all(), [])


if __name__ == "__main__":
    unittest.main()
