import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.models.OfferDiscountType import OfferDiscountType
from sqlmodel import Session, SQLModel, create_engine, select

from core.models.experiment import ExperimentConversion, ExperimentExposure
from core.models.transaction import Transaction
from core.models.user import User
from core.routers.subscriptions import (
    CreateTransactionRequest,
    ExperimentContext,
    create_or_update_transaction,
    get_experiment_summary,
    report_experiment_exposure,
)


class ExperimentReportingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(id="test-user")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    @staticmethod
    def context(variant: str = "treatment_simplified") -> ExperimentContext:
        installation_id = "11111111-2222-4333-8444-555555555555"
        return ExperimentContext(
            experiment_id="activation_experience_2026_08",
            variant=variant,
            eligible=True,
            installation_id=installation_id,
            exposure_id=f"activation_experience_2026_08:{installation_id}",
            app_version="3.4",
            build_number="43",
            analytics_environment="production",
            exposed_at_ms=1_786_976_300_000,
        )

    @staticmethod
    def decoded_transaction(transaction_id: str = "transaction-1"):
        return SimpleNamespace(
            originalTransactionId="original-1",
            transactionId=transaction_id,
            productId="com.zhirayr.Flighttracker.yearly.trial7d",
            purchaseDate=1_786_976_400_000,
            originalPurchaseDate=1_786_976_400_000,
            signedDate=1_786_976_401_000,
            expiresDate=1_787_581_200_000,
            transactionReason="PURCHASE",
            price=19990,
            currency="USD",
            isUpgraded=False,
            environment=Environment.PRODUCTION,
            revocationDate=None,
            offerDiscountType=OfferDiscountType.FREE_TRIAL,
            offerPeriod="P7D",
        )

    def test_verified_trial_is_joined_to_one_idempotent_exposure(self):
        context = self.context()
        self.assertEqual(
            report_experiment_exposure(context, self.user, self.session),
            {"detail": "success"},
        )
        self.assertEqual(
            report_experiment_exposure(context, self.user, self.session),
            {"detail": "success"},
        )

        request = CreateTransactionRequest(
            jws_payload="signed-jws",
            experiment=context,
        )
        with patch(
            "core.routers.subscriptions.AppStoreService.process_transaction",
            return_value=self.decoded_transaction(),
        ):
            self.assertEqual(
                create_or_update_transaction(request, self.user, self.session),
                {"detail": "successfull"},
            )

        exposures = self.session.exec(select(ExperimentExposure)).all()
        conversions = self.session.exec(select(ExperimentConversion)).all()
        self.assertEqual(len(exposures), 1)
        self.assertEqual(len(conversions), 1)
        self.assertTrue(conversions[0].starts_trial)
        self.assertEqual(conversions[0].trial_duration_days, 7)
        self.assertEqual(conversions[0].purchase_environment, "Production")

        summary = get_experiment_summary(
            "activation_experience_2026_08",
            app_version="3.4",
            session=self.session,
        )
        self.assertEqual(
            summary["arms"],
            [
                {
                    "variant": "treatment_simplified",
                    "exposed_installations": 1,
                    "verified_trial_installations": 1,
                    "verified_purchase_installations": 1,
                    "trial_conversion_rate": 1.0,
                    "purchase_conversion_rate": 1.0,
                }
            ],
        )

    def test_legacy_transaction_request_remains_supported(self):
        request = CreateTransactionRequest(jws_payload="legacy-jws")
        with patch(
            "core.routers.subscriptions.AppStoreService.process_transaction",
            return_value=self.decoded_transaction("legacy-transaction"),
        ):
            self.assertEqual(
                create_or_update_transaction(request, self.user, self.session),
                {"detail": "successfull"},
            )

        self.assertIsNotNone(self.session.get(Transaction, "legacy-transaction"))
        self.assertEqual(self.session.exec(select(ExperimentConversion)).all(), [])

    def test_purchase_registration_backfills_missing_exposure(self):
        context = self.context(variant="control_current")
        request = CreateTransactionRequest(
            jws_payload="signed-jws",
            experiment=context,
        )
        with patch(
            "core.routers.subscriptions.AppStoreService.process_transaction",
            return_value=self.decoded_transaction(),
        ):
            create_or_update_transaction(request, self.user, self.session)

        exposure = self.session.get(
            ExperimentExposure,
            "activation_experience_2026_08:11111111-2222-4333-8444-555555555555",
        )
        self.assertIsNotNone(exposure)
        self.assertEqual(exposure.source, "purchase_registration")

    def test_conversion_keeps_original_exposure_version_after_app_update(self):
        exposure_context = self.context()
        report_experiment_exposure(exposure_context, self.user, self.session)
        conversion_context = exposure_context.model_copy(
            update={
                "app_version": "3.5",
                "build_number": "44",
                "exposed_at_ms": None,
            }
        )
        request = CreateTransactionRequest(
            jws_payload="signed-jws",
            experiment=conversion_context,
        )
        with patch(
            "core.routers.subscriptions.AppStoreService.process_transaction",
            return_value=self.decoded_transaction(),
        ):
            create_or_update_transaction(request, self.user, self.session)

        conversion = self.session.get(ExperimentConversion, "transaction-1")
        self.assertEqual(conversion.app_version, "3.4")
        self.assertEqual(conversion.build_number, "43")
        self.assertEqual(conversion.conversion_app_version, "3.5")
        self.assertEqual(conversion.conversion_build_number, "44")
        summary = get_experiment_summary(
            "activation_experience_2026_08",
            app_version="3.4",
            session=self.session,
        )
        self.assertEqual(summary["arms"][0]["verified_trial_installations"], 1)

    def test_old_entitlement_without_exposure_timestamp_is_not_backfilled(self):
        context = self.context().model_copy(update={"exposed_at_ms": None})
        request = CreateTransactionRequest(
            jws_payload="signed-jws",
            experiment=context,
        )
        with patch(
            "core.routers.subscriptions.AppStoreService.process_transaction",
            return_value=self.decoded_transaction(),
        ):
            create_or_update_transaction(request, self.user, self.session)

        self.assertEqual(self.session.exec(select(ExperimentExposure)).all(), [])
        self.assertEqual(len(self.session.exec(select(ExperimentConversion)).all()), 1)
        summary = get_experiment_summary(
            "activation_experience_2026_08",
            app_version="3.4",
            session=self.session,
        )
        self.assertEqual(summary["arms"][0]["exposed_installations"], 0)
        self.assertEqual(summary["arms"][0]["verified_trial_installations"], 0)


if __name__ == "__main__":
    unittest.main()
