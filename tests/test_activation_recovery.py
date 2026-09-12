import os
import unittest
from pathlib import Path
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


from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

from core.models.activation_recovery import PurchaseActivationRecovery
from core.models.apple_ads import AppStoreRevenueEvent
from core.models.experiment import ExperimentConversion
from core.models.subscription import Subscription
from core.models.transaction import Transaction
from core.models.user import User, UserSubscriptionLink
from core.routers.subscriptions import (
    ActivationRecoveryRequest,
    get_activation_recovery_alerts,
    report_activation_recovery,
)
from core.services.activation_recovery import emit_due_activation_recovery_alerts


class ActivationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.session.add(self.user)
        self.session.add(Subscription(id="original-1"))
        self.session.add(
            Transaction(
                id="transaction-1",
                subscription_id="original-1",
                app_account_token=self.user.id,
                environment="Production",
            )
        )
        self.session.add(
            UserSubscriptionLink(
                user_id=self.user.id,
                subscription_id="original-1",
            )
        )
        self.session.add(
            ExperimentConversion(
                id="transaction-1",
                original_transaction_id="original-1",
                experiment_id="activation_experience_2026_08",
                variant="treatment_simplified",
                eligible=True,
                installation_id="installation-1",
                exposure_id="activation_experience_2026_08:installation-1",
                app_version="3.4",
                build_number="109",
                conversion_app_version="3.5",
                conversion_build_number="110",
                analytics_environment="production",
                user_id=self.user.id,
                product_id="yearly",
                purchase_environment="Production",
                starts_trial=True,
            )
        )
        self.session.add(AppStoreRevenueEvent(
            id="transaction-1", original_transaction_id="original-1", user_id=self.user.id,
            product_id="yearly", purchase_date_ms=1000, purchase_environment="Production",
            price_milliunits=0, currency="USD", app_account_token=self.user.id, starts_trial=True,
        ))
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    @staticmethod
    def pending_request() -> ActivationRecoveryRequest:
        return ActivationRecoveryRequest(
            transaction_id="transaction-1",
            state="recovery_pending",
            flight_id=42,
            failure_reason="backend_rejected",
        )

    def test_pending_alerts_once_after_five_minutes(self):
        with patch("core.routers.subscriptions.time.time", return_value=1_000):
            result = report_activation_recovery(
                self.pending_request(),
                self.user,
                self.session,
            )
        self.assertEqual(result["alert_due_at"], 1_300)

        with patch("core.routers.subscriptions.time.time", return_value=1_100):
            report_activation_recovery(
                self.pending_request(),
                self.user,
                self.session,
            )

        recovery = self.session.get(PurchaseActivationRecovery, "transaction-1")
        self.assertEqual(recovery.first_pending_at, 1_000)
        self.assertEqual(recovery.alert_due_at, 1_300)
        self.assertEqual(recovery.experiment_variant, "treatment_simplified")
        self.assertEqual(recovery.app_version, "3.5")

        self.assertEqual(
            emit_due_activation_recovery_alerts(self.session, now_seconds=1_299),
            0,
        )
        with patch("core.services.activation_recovery.logger.critical") as critical:
            self.assertEqual(
                emit_due_activation_recovery_alerts(self.session, now_seconds=1_300),
                1,
            )
            self.assertEqual(
                emit_due_activation_recovery_alerts(self.session, now_seconds=1_400),
                0,
            )
            critical.assert_called_once()

        alerts = get_activation_recovery_alerts(False, self.session)
        self.assertEqual(alerts["count"], 1)
        self.assertEqual(alerts["alerts"][0]["flight_id"], 42)

    def test_resolved_recovery_does_not_alert(self):
        with patch("core.routers.subscriptions.time.time", return_value=1_000):
            report_activation_recovery(
                self.pending_request(),
                self.user,
                self.session,
            )
        with patch("core.routers.subscriptions.time.time", return_value=1_120):
            result = report_activation_recovery(
                ActivationRecoveryRequest(
                    transaction_id="transaction-1",
                    state="resolved",
                    flight_id=42,
                ),
                self.user,
                self.session,
            )

        self.assertEqual(result["detail"], "resolved")
        self.assertEqual(
            emit_due_activation_recovery_alerts(self.session, now_seconds=2_000),
            0,
        )
        recovery = self.session.get(PurchaseActivationRecovery, "transaction-1")
        self.assertEqual(recovery.state, "resolved")
        self.assertEqual(recovery.resolved_at, 1_120)

    def test_other_user_cannot_report_recovery(self):
        other_user = User(id="cccccccc-cccc-4ccc-8ccc-cccccccccccc")
        self.session.add(other_user)
        self.session.commit()

        with self.assertRaises(HTTPException) as raised:
            report_activation_recovery(
                self.pending_request(),
                other_user,
                self.session,
            )
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(
            self.session.exec(select(PurchaseActivationRecovery)).all(),
            [],
        )

    def _restoring_user(self):
        other = User(id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", premium_valid_until=9_999_999_999)
        self.session.add(other)
        self.session.add(UserSubscriptionLink(user_id=other.id, subscription_id="original-1"))
        self.session.commit()
        return other

    def _assert_owner_failure_preserves_all_facts(self, user, state):
        transaction_before = self.session.get(Transaction, "transaction-1").model_dump()
        conversion_before = self.session.get(ExperimentConversion, "transaction-1").model_dump()
        revenue_before = self.session.get(AppStoreRevenueEvent, "transaction-1").model_dump()
        user_before = self.session.get(User, user.id).model_dump()
        links_before = sorted((row.user_id, row.subscription_id) for row in self.session.exec(select(UserSubscriptionLink)).all())
        recovery = self.session.get(PurchaseActivationRecovery, "transaction-1")
        recovery_before = recovery.model_dump() if recovery else None
        with self.assertRaises(HTTPException) as failure:
            report_activation_recovery(
                ActivationRecoveryRequest(transaction_id="transaction-1", state=state,
                    flight_id=999, failure_reason="backend_rejected" if state == "recovery_pending" else None),
                user, self.session,
            )
        self.assertEqual(failure.exception.status_code, 404)
        self.assertEqual(failure.exception.detail, "Verified transaction was not found")
        recovery = self.session.get(PurchaseActivationRecovery, "transaction-1", populate_existing=True)
        self.assertEqual(recovery.model_dump() if recovery else None, recovery_before)
        self.assertEqual(self.session.get(Transaction, "transaction-1", populate_existing=True).model_dump(), transaction_before)
        self.assertEqual(self.session.get(ExperimentConversion, "transaction-1", populate_existing=True).model_dump(), conversion_before)
        self.assertEqual(self.session.get(AppStoreRevenueEvent, "transaction-1", populate_existing=True).model_dump(), revenue_before)
        self.assertEqual(self.session.get(User, user.id, populate_existing=True).model_dump(), user_before)
        self.assertEqual(sorted((row.user_id, row.subscription_id) for row in self.session.exec(select(UserSubscriptionLink)).all()), links_before)

    def test_restored_entitlement_cannot_create_or_resolve_missing_original_owner_row(self):
        restoring = self._restoring_user()
        for state in ("recovery_pending", "resolved"):
            with self.subTest(state=state):
                self._assert_owner_failure_preserves_all_facts(restoring, state)

    def test_restored_entitlement_cannot_mutate_existing_original_owner_row(self):
        restoring = self._restoring_user()
        report_activation_recovery(self.pending_request(), self.user, self.session)
        for state in ("recovery_pending", "resolved"):
            with self.subTest(state=state):
                self._assert_owner_failure_preserves_all_facts(restoring, state)
        # Original ownership still allows normal resolution, despite B's valid
        # entitlement link. B cannot use the already-resolved response either.
        result = report_activation_recovery(ActivationRecoveryRequest(transaction_id="transaction-1", state="resolved", flight_id=42), self.user, self.session)
        self.assertEqual(result["detail"], "resolved")
        for state in ("recovery_pending", "resolved"):
            with self.subTest(existing="resolved", state=state):
                self._assert_owner_failure_preserves_all_facts(restoring, state)

    def test_original_transaction_owner_cannot_overwrite_another_recovery_row_owner(self):
        restoring = self._restoring_user()
        # Reproduce an older/corrupt row created under entitlement-only checks.
        report_activation_recovery(self.pending_request(), self.user, self.session)
        row = self.session.get(PurchaseActivationRecovery, "transaction-1")
        row.user_id = restoring.id
        self.session.add(row)
        self.session.commit()
        for state in ("recovery_pending", "resolved"):
            with self.subTest(state=state):
                self._assert_owner_failure_preserves_all_facts(self.user, state)

    def test_missing_transaction_owner_is_not_inferred_from_entitlement_link(self):
        transaction = self.session.get(Transaction, "transaction-1")
        transaction.app_account_token = None
        self.session.add(transaction)
        self.session.commit()
        for state in ("recovery_pending", "resolved"):
            with self.subTest(state=state):
                self._assert_owner_failure_preserves_all_facts(self.user, state)

    def test_malformed_transaction_or_recovery_owner_is_not_trusted(self):
        transaction = self.session.get(Transaction, "transaction-1")
        transaction.app_account_token = "not-a-uuid"
        self.session.add(transaction)
        self.session.commit()
        for state in ("recovery_pending", "resolved"):
            self._assert_owner_failure_preserves_all_facts(self.user, state)
        transaction.app_account_token = self.user.id
        self.session.add(transaction)
        self.session.commit()
        report_activation_recovery(self.pending_request(), self.user, self.session)
        row = self.session.get(PurchaseActivationRecovery, "transaction-1")
        row.user_id = "not-a-uuid"
        self.session.add(row)
        self.session.commit()
        for state in ("recovery_pending", "resolved"):
            self._assert_owner_failure_preserves_all_facts(self.user, state)

    def test_equivalent_uuid_owner_casing_and_format_remain_authorized(self):
        transaction = self.session.get(Transaction, "transaction-1")
        transaction.app_account_token = self.user.id.upper().replace("-", "")
        self.session.add(transaction)
        self.session.commit()
        self.assertEqual(report_activation_recovery(self.pending_request(), self.user, self.session)["detail"], "recovery_pending")
        row = self.session.get(PurchaseActivationRecovery, "transaction-1")
        row.user_id = self.user.id.upper()
        self.session.add(row)
        self.session.commit()
        result = report_activation_recovery(ActivationRecoveryRequest(transaction_id="transaction-1", state="resolved", flight_id=42), self.user, self.session)
        self.assertEqual(result["detail"], "resolved")
        self.assertEqual(self.session.get(PurchaseActivationRecovery, "transaction-1").user_id, self.user.id.upper())

    def test_original_owner_does_not_need_a_restore_entitlement_link_for_recovery(self):
        link = self.session.exec(select(UserSubscriptionLink).where(UserSubscriptionLink.user_id == self.user.id)).one()
        self.session.delete(link)
        self.session.commit()
        result = report_activation_recovery(self.pending_request(), self.user, self.session)
        self.assertEqual(result["detail"], "recovery_pending")
        result = report_activation_recovery(ActivationRecoveryRequest(transaction_id="transaction-1", state="resolved", flight_id=42), self.user, self.session)
        self.assertEqual(result["detail"], "resolved")

    def test_nonproduction_purchase_is_ignored(self):
        transaction = self.session.get(Transaction, "transaction-1")
        transaction.environment = "Sandbox"
        self.session.add(transaction)
        self.session.commit()

        result = report_activation_recovery(
            self.pending_request(),
            self.user,
            self.session,
        )
        self.assertEqual(result["detail"], "ignored_non_production")
        self.assertIsNone(
            self.session.get(PurchaseActivationRecovery, "transaction-1")
        )


if __name__ == "__main__":
    unittest.main()
