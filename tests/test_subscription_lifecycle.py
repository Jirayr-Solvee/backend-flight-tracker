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
    "MAX_PREMIUM_HOURS": "24",
    "APPLE_ISSUER": "test",
    "APPLE_KEYS_URL": "https://example.invalid",
    "GUEST_KEY": "test",
    "APN_KEY_PATH": str(TEST_FILE),
    "APPLE_ROOT_CERT_PATH": str(TEST_FILE),
    "AIRLINE_MAP_JSON": str(REPOSITORY_ROOT / "iata_to_icao.json"),
}.items():
    os.environ.setdefault(key, value)


from sqlmodel import Session, SQLModel, create_engine, select

from core.models.experiment import ExperimentConversion, ExperimentExposure
from core.models.subscription import Subscription
from core.models.subscription_lifecycle import AppStoreSubscriptionLifecycleEvent
from core.models.transaction import Transaction
from core.models.apple_ads import AppStoreRevenueEvent
from core.models.user import User
from core.routers.subscriptions import (
    get_experiment_lifecycle_summary, CreateTransactionRequest,
    create_or_update_transaction as register_transaction,
)
from core.routers.webhook import (
    CreateOrUpdateTransactionRequest,
    create_or_update_transaction,
)
from core.services.subscription_lifecycle import (
    lifecycle_metrics,
    upsert_subscription_lifecycle_event,
)


class SubscriptionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    @staticmethod
    def transaction(
        *,
        transaction_id: str = "transaction-1",
        signed_date: int = 2_000,
        revoked_date: int | None = None,
    ):
        return SimpleNamespace(
            originalTransactionId="original-1",
            transactionId=transaction_id,
            appAccountToken="user-1",
            productId="com.zhirayr.Flighttracker.yearly.trial7d",
            purchaseDate=1_000,
            originalPurchaseDate=1_000,
            signedDate=signed_date,
            expiresDate=10_000_000_000_000,
            transactionReason="RENEWAL",
            price=44_990,
            currency="EUR",
            isUpgraded=False,
            environment="Production",
            revocationDate=revoked_date,
            revocationReason=1 if revoked_date else None,
            rawRevocationReason=None,
            offerDiscountType=None,
            offerPeriod=None,
        )

    @staticmethod
    def renewal_info(
        *,
        auto_renew_status: int = 1,
        billing_retry: bool = False,
        grace_period_expires_date: int | None = None,
    ):
        return SimpleNamespace(
            originalTransactionId="original-1",
            appAccountToken="user-1",
            productId="com.zhirayr.Flighttracker.yearly.trial7d",
            environment="Production",
            signedDate=3_000,
            renewalDate=10_000_000_000_000,
            autoRenewStatus=auto_renew_status,
            rawAutoRenewStatus=None,
            expirationIntent=None,
            rawExpirationIntent=None,
            isInBillingRetryPeriod=billing_retry,
            gracePeriodExpiresDate=grace_period_expires_date,
            renewalPrice=44_990,
            currency="EUR",
        )

    @staticmethod
    def notification(
        *,
        uuid: str,
        notification_type: str,
        subtype: str | None = None,
        status: int = 1,
    ):
        return SimpleNamespace(
            notificationUUID=uuid,
            notificationType=notification_type,
            rawNotificationType=None,
            subtype=subtype,
            rawSubtype=None,
            signedDate=4_000,
            data=SimpleNamespace(
                environment="Production",
                rawEnvironment=None,
                status=status,
                rawStatus=None,
                signedTransactionInfo="transaction-jws",
                signedRenewalInfo="renewal-jws",
            ),
        )

    def test_requested_lifecycle_states_are_classified_and_idempotent(self):
        cases = (
            (
                "DID_CHANGE_RENEWAL_STATUS",
                "AUTO_RENEW_DISABLED",
                "auto_renew_disabled",
                {"auto_renew_disabled"},
            ),
            ("DID_RENEW", None, "renewal", {"renewal"}),
            ("EXPIRED", "VOLUNTARY", "expiration", {"expiration"}),
            (
                "DID_FAIL_TO_RENEW",
                "BILLING_RETRY",
                "billing_failure",
                {"billing_failure"},
            ),
            (
                "DID_FAIL_TO_RENEW",
                "GRACE_PERIOD",
                "grace_period_started",
                {"billing_failure", "grace_period_started"},
            ),
            (
                "GRACE_PERIOD_EXPIRED",
                None,
                "grace_period_expired",
                {"grace_period_expired"},
            ),
            ("REFUND", None, "refund", {"refund"}),
        )

        for index, (notification_type, subtype, kind, metrics) in enumerate(cases):
            notification = self.notification(
                uuid=f"notification-{index}",
                notification_type=notification_type,
                subtype=subtype,
            )
            event = upsert_subscription_lifecycle_event(
                session=self.session,
                notification=notification,
                decoded_transaction=self.transaction(),
                decoded_renewal_info=self.renewal_info(
                    auto_renew_status=(
                        0 if subtype == "AUTO_RENEW_DISABLED" else 1
                    ),
                    billing_retry=notification_type == "DID_FAIL_TO_RENEW",
                    grace_period_expires_date=(
                        20_000 if subtype == "GRACE_PERIOD" else None
                    ),
                ),
                user_id="user-1",
            )
            self.assertEqual(event.event_kind, kind)
            self.assertEqual(lifecycle_metrics(event), metrics)

        self.session.commit()
        duplicate = upsert_subscription_lifecycle_event(
            session=self.session,
            notification=self.notification(
                uuid="notification-0",
                notification_type="DID_CHANGE_RENEWAL_STATUS",
                subtype="AUTO_RENEW_DISABLED",
            ),
            decoded_transaction=self.transaction(),
            decoded_renewal_info=self.renewal_info(auto_renew_status=0),
            user_id="user-1",
        )
        self.session.commit()
        self.assertEqual(duplicate.auto_renew_status, 0)
        self.assertEqual(
            len(self.session.exec(select(AppStoreSubscriptionLifecycleEvent)).all()),
            len(cases),
        )

    def test_webhook_records_status_change_when_transaction_is_not_newer(self):
        user = User(id="user-1", premium_valid_until=10_000_000_000_000)
        subscription = Subscription(id="original-1")
        transaction = Transaction(
            id="transaction-1",
            subscription_id="original-1",
            signed_date=5_000,
        )
        self.session.add(user)
        self.session.add(subscription)
        self.session.add(transaction)
        self.session.commit()

        notification = self.notification(
            uuid="renew-disabled-1",
            notification_type="DID_CHANGE_RENEWAL_STATUS",
            subtype="AUTO_RENEW_DISABLED",
        )
        with (
            patch(
                "core.routers.webhook.AppStoreService.process_notification",
                return_value=notification,
            ),
            patch(
                "core.routers.webhook.AppStoreService.process_transaction",
                return_value=self.transaction(signed_date=2_000),
            ),
            patch(
                "core.routers.webhook.AppStoreService.process_renewal_info",
                return_value=self.renewal_info(auto_renew_status=0),
            ),
        ):
            result = create_or_update_transaction(
                CreateOrUpdateTransactionRequest(signedPayload="notification-jws"),
                session=self.session,
            )

        self.assertEqual(result, {"detail": "ok"})
        stored_transaction = self.session.get(Transaction, "transaction-1")
        self.assertEqual(stored_transaction.signed_date, 5_000)
        event = self.session.get(
            AppStoreSubscriptionLifecycleEvent, "renew-disabled-1"
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.event_kind, "auto_renew_disabled")
        self.session.refresh(user)
        self.assertEqual(user.premium_valid_until, 10_000_000_000_000)

    def test_refund_clears_entitlement_even_when_transaction_jws_repeats(self):
        user = User(id="user-1", premium_valid_until=10_000_000_000_000)
        subscription = Subscription(id="original-1")
        transaction = Transaction(
            id="transaction-1",
            subscription_id="original-1",
            signed_date=5_000,
        )
        user.subscriptions.append(subscription)
        self.session.add(user)
        self.session.add(transaction)
        self.session.commit()

        notification = self.notification(
            uuid="refund-1",
            notification_type="REFUND",
            status=5,
        )
        with (
            patch(
                "core.routers.webhook.AppStoreService.process_notification",
                return_value=notification,
            ),
            patch(
                "core.routers.webhook.AppStoreService.process_transaction",
                return_value=self.transaction(
                    signed_date=5_000,
                    revoked_date=6_000,
                ),
            ),
            patch(
                "core.routers.webhook.AppStoreService.process_renewal_info",
                return_value=self.renewal_info(auto_renew_status=0),
            ),
        ):
            result = create_or_update_transaction(
                CreateOrUpdateTransactionRequest(signedPayload="notification-jws"),
                session=self.session,
            )

        self.assertEqual(result, {"detail": "ok"})
        self.session.refresh(user)
        self.assertIsNone(user.premium_valid_until)
        event = self.session.get(AppStoreSubscriptionLifecycleEvent, "refund-1")
        self.assertEqual(event.event_kind, "refund")
        self.assertEqual(self.session.get(Transaction, "transaction-1").revoked_date, 6_000)
        self.assertEqual(self.session.get(AppStoreRevenueEvent, "transaction-1").revoked_date_ms, 6_000)

    def test_renewal_info_only_notification_is_still_captured(self):
        self.session.add(User(id="user-1"))
        self.session.commit()
        notification = self.notification(
            uuid="renewal-info-only-1",
            notification_type="DID_CHANGE_RENEWAL_STATUS",
            subtype="AUTO_RENEW_DISABLED",
        )
        notification.data.signedTransactionInfo = None

        with (
            patch(
                "core.routers.webhook.AppStoreService.process_notification",
                return_value=notification,
            ),
            patch(
                "core.routers.webhook.AppStoreService.process_transaction"
            ) as process_transaction,
            patch(
                "core.routers.webhook.AppStoreService.process_renewal_info",
                return_value=self.renewal_info(auto_renew_status=0),
            ),
        ):
            result = create_or_update_transaction(
                CreateOrUpdateTransactionRequest(signedPayload="notification-jws"),
                session=self.session,
            )

        self.assertEqual(result, {"detail": "ok"})
        process_transaction.assert_not_called()
        event = self.session.get(
            AppStoreSubscriptionLifecycleEvent, "renewal-info-only-1"
        )
        self.assertEqual(event.event_kind, "auto_renew_disabled")
        self.assertEqual(event.original_transaction_id, "original-1")
        self.assertEqual(event.auto_renew_status, 0)

    def register(self, transaction):
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=transaction):
            return register_transaction(CreateTransactionRequest(jws_payload="verified-fixture"),
                                        self.session.get(User, "user-1"), self.session)

    def notify(self, notification, transaction, renewal):
        with (
            patch("core.routers.webhook.AppStoreService.process_notification", return_value=notification),
            patch("core.routers.webhook.AppStoreService.process_transaction", return_value=transaction),
            patch("core.routers.webhook.AppStoreService.process_renewal_info", return_value=renewal),
        ):
            return create_or_update_transaction(
                CreateOrUpdateTransactionRequest(signedPayload="verified-fixture"), self.session)

    def test_resigned_expired_transaction_preserves_independent_grace_then_expiration(self):
        user = User(id="user-1")
        self.session.add(user)
        self.session.commit()
        transaction = self.transaction(signed_date=2_000)
        transaction.expiresDate = 1_000
        self.register(transaction)
        self.notify(self.notification(uuid="grace", notification_type="DID_FAIL_TO_RENEW",
                                      subtype="GRACE_PERIOD", status=4), transaction,
                    self.renewal_info(billing_retry=True, grace_period_expires_date=10_000_000_000_000))
        self.session.refresh(user)
        self.assertIsNotNone(user.premium_valid_until)
        transaction.signedDate = 5_000
        self.register(transaction)
        self.session.refresh(user)
        self.assertIsNotNone(user.premium_valid_until)

        information = self.notification(uuid="consumption-request", notification_type="CONSUMPTION_REQUEST", status=4)
        information.signedDate = 5_500
        information.data.signedRenewalInfo = None
        self.notify(information, transaction, None)
        self.register(transaction)
        self.session.refresh(user)
        self.assertIsNotNone(user.premium_valid_until)

        expired = self.notification(uuid="grace-expired", notification_type="GRACE_PERIOD_EXPIRED", status=2)
        expired.signedDate = 6_000
        self.notify(expired, transaction, self.renewal_info(billing_retry=True))
        transaction.signedDate = 7_000
        self.register(transaction)
        self.session.refresh(user)
        self.assertIsNone(user.premium_valid_until)

    def test_newer_refund_of_historical_transaction_does_not_hide_current_grace(self):
        user = User(id="user-1")
        self.session.add(user)
        self.session.commit()
        first = self.transaction(transaction_id="T1", signed_date=2_000)
        first.expiresDate = 2_000
        self.register(first)
        current = self.transaction(transaction_id="T2", signed_date=3_000)
        current.purchaseDate = 2_000
        current.expiresDate = 3_000
        self.register(current)
        self.notify(self.notification(uuid="current-grace", notification_type="DID_FAIL_TO_RENEW",
                                      subtype="GRACE_PERIOD", status=4), current,
                    self.renewal_info(billing_retry=True, grace_period_expires_date=10_000_000_000_000))
        self.session.refresh(user)
        self.assertIsNotNone(user.premium_valid_until)

        refund = self.notification(uuid="historical-refund", notification_type="REFUND", status=4)
        refund.signedDate = 6_000
        refund.data.signedRenewalInfo = None
        first.signedDate = 6_000
        first.revocationDate = 5_000
        self.notify(refund, first, None)
        self.session.refresh(user)
        self.assertIsNotNone(user.premium_valid_until)
        self.assertEqual(self.session.get(Transaction, "T1").revoked_date, 5_000)
        self.assertIsNone(self.session.get(Transaction, "T2").revoked_date)

    def test_old_renewal_info_only_state_cannot_expire_later_renewal(self):
        user = User(id="user-1")
        self.session.add(user)
        self.session.commit()
        transaction = self.transaction()
        transaction.purchaseDate = 5_000
        transaction.signedDate = 6_000
        self.register(transaction)
        expired = self.notification(uuid="historical-renewal-only", notification_type="EXPIRED", status=2)
        expired.data.signedTransactionInfo = None
        self.notify(expired, None, self.renewal_info())
        self.register(transaction)
        self.session.refresh(user)
        self.assertIsNotNone(user.premium_valid_until)

    def test_experiment_lifecycle_summary_counts_events_and_subscriptions(self):
        exposure = ExperimentExposure(
            id="activation_experience_2026_08:installation-1",
            experiment_id="activation_experience_2026_08",
            variant="treatment_simplified",
            eligible=True,
            installation_id="11111111-2222-4333-8444-555555555555",
            app_version="3.4",
            build_number="109",
            analytics_environment="production",
            user_id="user-1",
            source="onboarding_exposure",
            exposed_at_ms=1_000,
        )
        conversion = ExperimentConversion(
            id="transaction-1",
            original_transaction_id="original-1",
            experiment_id="activation_experience_2026_08",
            variant="treatment_simplified",
            eligible=True,
            installation_id="11111111-2222-4333-8444-555555555555",
            exposure_id=exposure.id,
            app_version="3.4",
            build_number="109",
            conversion_app_version="3.4",
            conversion_build_number="109",
            analytics_environment="production",
            user_id="user-1",
            product_id="yearly",
            purchase_environment="Production",
            starts_trial=True,
        )
        self.session.add(exposure)
        self.session.add(conversion)
        for index, (notification_type, subtype) in enumerate(
            (
                ("DID_FAIL_TO_RENEW", "GRACE_PERIOD"),
                ("DID_RENEW", "BILLING_RECOVERY"),
                ("REFUND", None),
            )
        ):
            upsert_subscription_lifecycle_event(
                session=self.session,
                notification=self.notification(
                    uuid=f"summary-{index}",
                    notification_type=notification_type,
                    subtype=subtype,
                ),
                decoded_transaction=self.transaction(
                    transaction_id=f"summary-transaction-{index}"
                ),
                decoded_renewal_info=self.renewal_info(),
                user_id="user-1",
            )
        self.session.commit()

        summary = get_experiment_lifecycle_summary(
            "activation_experience_2026_08",
            app_version="3.4",
            session=self.session,
        )
        arm = summary["arms"][0]
        self.assertEqual(arm["variant"], "treatment_simplified")
        self.assertEqual(arm["tracked_subscriptions"], 1)
        self.assertEqual(arm["events"]["billing_failure"], 1)
        self.assertEqual(arm["events"]["grace_period_started"], 1)
        self.assertEqual(arm["events"]["renewal"], 1)
        self.assertEqual(arm["events"]["refund"], 1)
        self.assertEqual(arm["affected_subscriptions"]["refund"], 1)


if __name__ == "__main__":
    unittest.main()
