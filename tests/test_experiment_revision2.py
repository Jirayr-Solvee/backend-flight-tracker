import unittest
import tempfile
from uuid import uuid4
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from tests import test_experiment_reporting as fixtures
from core.config import settings
from core.models.apple_ads import AppStoreRevenueEvent
from core.models.transaction import Transaction
from core.models.user import User
from core.models.experiment import ExperimentDiagnosticEvent, ExperimentEnrollment, current_time_ms
from core.routers.experiment_diagnostics import (
    DiagnosticBatch, DiagnosticEvent, get_diagnostic_report, record_diagnostic_events, router,
)
from core.routers.subscriptions import (
    CreateTransactionRequest, ExperimentAssignmentRequest, ExperimentEnrollmentRequest,
    create_or_update_transaction, get_experiment_assignment, get_experiment_summary,
    report_experiment_enrollment, report_experiment_exposure,
)
from core.services.experiment_reporting import experiment_summary
from core.models import get_session
from core.utils import create_jwt


class RevisedExperimentTests(unittest.TestCase):
    setUp = fixtures.ExperimentReportingTests.setUp
    tearDown = fixtures.ExperimentReportingTests.tearDown
    DAY = 86_400_000
    START = 1_787_000_000_000

    def context(self, variant="control_current_paywall", *, production=True):
        context = fixtures.ExperimentReportingTests.context()
        installation = uuid4()
        return context.model_copy(update={
            "experiment_id": "paywall_flight_detail_2026_09", "variant": variant,
            "installation_id": installation,
            "exposure_id": f"paywall_flight_detail_2026_09:{installation}",
            "app_version": "3.7", "build_number": "117", "measurement_revision": 2,
            "analytics_environment": "production" if production else "development",
            "exposed_at_ms": self.START,
        })

    def enroll(self, context):
        return report_experiment_enrollment(ExperimentEnrollmentRequest(
            experiment=context, measurement_revision=2, enrolled_at_ms=self.START,
        ), self.user, self.session)

    def trial(self, context, id="trial-v2"):
        decoded = fixtures.ExperimentReportingTests.decoded_transaction(id)
        decoded.purchaseDate = self.START + 1_000
        decoded.expiresDate = decoded.purchaseDate + 7 * self.DAY
        decoded.price = 0
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
            create_or_update_transaction(CreateTransactionRequest(jws_payload="verified-fixture", experiment=context), self.user, self.session)
        return decoded

    def summary(self, **kwargs):
        return experiment_summary(
            session=self.session, experiment_id="paywall_flight_detail_2026_09",
            app_version="3.7", measurement_revision=kwargs.pop("measurement_revision", 2),
            since_ms=kwargs.pop("since_ms", None), until_ms=None,
            product_id=kwargs.pop("product_id", None), acquisition_source=None,
            horizon_days=14, as_of_ms=kwargs.pop("as_of_ms", self.START + 15 * self.DAY),
            **kwargs,
        )

    def test_control_abandoning_preview_stays_in_denominator_and_legacy_is_separate(self):
        first, second, legacy = self.context(), self.context(), self.context()
        self.enroll(first)
        self.enroll(second)
        self.enroll(first)
        self.assertEqual(len(self.session.exec(select(ExperimentEnrollment)).all()), 2)
        self.trial(first)
        report_experiment_exposure(legacy, self.user, self.session)
        arm = self.summary()["arms"][0]
        self.assertEqual(arm["eligible_installations"], 2)
        self.assertEqual(arm["trial_conversion_rate"], 0.5)
        self.assertEqual(arm["paid_conversion_rate"], 0)
        self.assertEqual(arm["actual_paywall_exposed_installations"], 0)
        self.assertEqual(arm["acquisition_sources"], {"apple_ads": 0, "unknown": 2})
        self.assertEqual(self.summary(measurement_revision=1)["arms"][0]["eligible_installations"], 1)

    def test_renewal_after_day_seven_is_paid_and_maturity_is_not_trial_start(self):
        context = self.context()
        self.enroll(context)
        trial = self.trial(context)
        self.session.add(AppStoreRevenueEvent(
            id="paid-renewal", original_transaction_id=trial.originalTransactionId,
            user_id=self.user.id, product_id=trial.productId,
            purchase_date_ms=trial.expiresDate, purchase_environment="Production",
            currency="USD", price_milliunits=49990, starts_trial=False,
            revoked_date_ms=trial.expiresDate + 1_000, revocation_percentage=20_000,
        ))
        self.session.commit()
        before = self.summary(as_of_ms=self.START + 6 * self.DAY)["arms"][0]
        self.assertEqual(before["matured_trial_installations"], 0)
        self.assertIsNone(before["matured_trial_to_paid_rate"])
        self.assertEqual(before["paid_installations"], 0)
        after = self.summary()["arms"][0]
        self.assertEqual(after["matured_trial_to_paid_rate"], 1)
        self.assertEqual(after["paid_installations"], 1)
        self.assertEqual(after["mature_horizon_revenue"][0]["gross_revenue"], 49.99)
        self.assertAlmostEqual(after["mature_horizon_revenue"][0]["refunds"], 9.998)

    def test_product_filter_does_not_select_only_converters_and_dates_filter_enrollment(self):
        context, abandoned, debug = self.context(), self.context(), self.context(production=False)
        self.enroll(context)
        self.enroll(abandoned)
        self.enroll(debug)
        trial = self.trial(context)
        arm = self.summary(product_id=trial.productId)["arms"][0]
        self.assertEqual(arm["eligible_installations"], 2)
        self.assertEqual(arm["trial_conversion_rate"], 0.5)
        self.assertEqual(self.summary(since_ms=self.START + 1)["arms"], [])

    def test_partial_refund_millipercent_boundaries_in_experiment_report(self):
        context = self.context()
        self.enroll(context)
        trial = self.trial(context)
        paid = AppStoreRevenueEvent(
            id="partial-paid", original_transaction_id=trial.originalTransactionId,
            user_id=self.user.id, product_id=trial.productId,
            purchase_date_ms=trial.expiresDate, purchase_environment="Production",
            currency="USD", price_milliunits=40_000, starts_trial=False,
            revoked_date_ms=trial.expiresDate + 1_000,
        )
        for percentage, refunds in ((50_000, 20), (0, 0), (100_000, 40), (None, 40)):
            with self.subTest(percentage=percentage):
                paid.revocation_percentage = percentage
                self.session.add(paid)
                self.session.commit()
                money = self.summary()["arms"][0]["mature_horizon_revenue"][0]
                self.assertEqual(money["gross_revenue"], 40)
                self.assertEqual(money["refunds"], refunds)

    def test_enrollment_conflict_does_not_reassign_existing_cohort(self):
        context = self.context()
        self.enroll(context)
        with self.assertRaises(HTTPException) as caught:
            self.enroll(context.model_copy(update={"variant": "treatment_flight_detail_card"}))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.summary()["arms"][0]["variant"], "control_current_paywall")

    def test_kill_switch_has_effect_without_mutating_locked_assignment(self):
        request = ExperimentAssignmentRequest(
            experiment_id="paywall_flight_detail_2026_09", installation_id=uuid4(),
            current_variant="treatment_flight_detail_card", assignment_locked=True,
            app_version="3.7", build_number="117", analytics_environment="production",
        )
        with patch.object(settings, "FLIGHT_DETAIL_PAYWALL_EXPERIMENT_MODE", "off"):
            response = get_experiment_assignment(request, self.user)
        self.assertEqual(response.variant, "treatment_flight_detail_card")
        self.assertEqual(response.effective_variant, "control_current_paywall")
        self.assertFalse(response.experiment_enabled)

    def test_verified_payment_survives_conflicting_experiment_context(self):
        context = self.context()
        self.enroll(context)
        bad_context = context.model_copy(update={"variant": "treatment_flight_detail_card"})
        decoded = fixtures.ExperimentReportingTests.decoded_transaction()
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
            result = create_or_update_transaction(CreateTransactionRequest(
                jws_payload="verified-fixture", experiment=bad_context), self.user, self.session)
        self.assertEqual(result, {"detail": "successfull", "experiment_tracking_status": "conflict"})
        # Expire local identity state and read again to prove the purchase's
        # independent commit survived the metadata rollback.
        self.session.expire_all()
        self.assertIsNotNone(self.session.get(Transaction, decoded.transactionId))
        self.assertIsNotNone(self.session.get(AppStoreRevenueEvent, decoded.transactionId))

    def test_verified_payment_survives_metadata_storage_failure_and_retry_recovers(self):
        context = self.context()
        self.enroll(context)
        decoded = fixtures.ExperimentReportingTests.decoded_transaction()
        decoded.purchaseDate = self.START + 1_000
        request = CreateTransactionRequest(jws_payload="verified-fixture", experiment=context)
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
            with patch("core.routers.subscriptions._record_experiment_conversion", side_effect=RuntimeError("injected metadata failure")):
                result = create_or_update_transaction(request, self.user, self.session)
            self.assertEqual(result["experiment_tracking_status"], "pending")
            self.session.expire_all()
            self.assertIsNotNone(self.session.get(Transaction, decoded.transactionId))
            result = create_or_update_transaction(request, self.user, self.session)
        self.assertEqual(result, {"detail": "successfull"})
        self.assertEqual(self.summary()["arms"][0]["verified_trial_installations"], 1)

    def test_old_or_equal_unrevoked_client_payload_cannot_erase_verified_refund(self):
        original = fixtures.ExperimentReportingTests.decoded_transaction()
        original.expiresDate = 10_000_000_000_000
        refunded = fixtures.ExperimentReportingTests.decoded_transaction()
        refunded.signedDate = original.signedDate + 10
        refunded.revocationDate = original.signedDate + 5
        request = CreateTransactionRequest(jws_payload="verified-fixture")
        for decoded in (original, refunded, original):
            with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
                create_or_update_transaction(request, self.user, self.session)
        self.session.expire_all()
        self.assertEqual(self.session.get(Transaction, original.transactionId).revoked_date, refunded.revocationDate)
        self.assertEqual(self.session.get(AppStoreRevenueEvent, original.transactionId).revoked_date_ms, refunded.revocationDate)
        self.assertIsNone(self.user.premium_valid_until)
        original.signedDate = refunded.signedDate
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=original):
            create_or_update_transaction(request, self.user, self.session)
        self.assertEqual(self.session.get(Transaction, original.transactionId).revoked_date, refunded.revocationDate)
        self.assertIsNone(self.user.premium_valid_until)

    def test_conversion_retains_enrollment_version_when_paywall_occurs_after_app_update(self):
        context = self.context()
        self.enroll(context)
        later = context.model_copy(update={"app_version": "3.8", "build_number": "118", "exposed_at_ms": self.START + 500})
        report_experiment_exposure(later, self.user, self.session)
        self.trial(later)
        from core.models.experiment import ExperimentConversion
        conversion = self.session.get(ExperimentConversion, "trial-v2")
        self.assertEqual(conversion.app_version, "3.7")
        self.assertEqual(conversion.conversion_app_version, "3.8")

    def test_verified_sandbox_transaction_cannot_enter_production_conversion_report(self):
        context = self.context()
        self.enroll(context)
        decoded = fixtures.ExperimentReportingTests.decoded_transaction()
        decoded.purchaseDate = self.START + 1_000
        decoded.environment = "Sandbox"
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
            create_or_update_transaction(CreateTransactionRequest(jws_payload="sandbox-fixture", experiment=context), self.user, self.session)
        arm = self.summary()["arms"][0]
        self.assertEqual(arm["verified_trial_installations"], 0)
        self.assertEqual(arm["paid_installations"], 0)

    def test_invalid_apple_signature_creates_no_transaction_or_financial_fact(self):
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=None):
            with self.assertRaises(HTTPException) as caught:
                create_or_update_transaction(CreateTransactionRequest(jws_payload="invalid-fixture"), self.user, self.session)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.session.exec(select(Transaction)).all(), [])
        self.assertEqual(self.session.exec(select(AppStoreRevenueEvent)).all(), [])

    def test_same_verified_snapshot_refreshes_entitlement_and_restores_new_guest(self):
        decoded = fixtures.ExperimentReportingTests.decoded_transaction()
        decoded.expiresDate = 10_000_000_000_000
        request = CreateTransactionRequest(jws_payload="verified-fixture")
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
            create_or_update_transaction(request, self.user, self.session)
            self.user.premium_valid_until = None
            self.session.add(self.user)
            self.session.commit()
            create_or_update_transaction(request, self.user, self.session)
            self.assertIsNotNone(self.user.premium_valid_until)
            restored_guest = User(id="restored-guest")
            self.session.add(restored_guest)
            self.session.commit()
            create_or_update_transaction(request, restored_guest, self.session)
            self.assertIsNotNone(restored_guest.premium_valid_until)

    def test_stale_independent_session_cannot_overwrite_newer_refund_commit(self):
        with tempfile.TemporaryDirectory(prefix="sofly-payment-concurrency-") as directory:
            engine = create_engine(f"sqlite:///{directory}/payments.sqlite")
            SQLModel.metadata.create_all(engine)
            original = fixtures.ExperimentReportingTests.decoded_transaction()
            original.expiresDate = 10_000_000_000_000
            refunded = fixtures.ExperimentReportingTests.decoded_transaction()
            refunded.signedDate += 1_000
            refunded.revocationDate = refunded.signedDate
            request = CreateTransactionRequest(jws_payload="verified-fixture")
            with Session(engine) as session:
                user = User(id="concurrent-user")
                session.add(user)
                session.commit()
                with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=original):
                    create_or_update_transaction(request, user, session)
            with Session(engine) as older:
                old_user = older.get(User, "concurrent-user")
                old_transaction = older.get(Transaction, original.transactionId)
                old_revenue = older.get(AppStoreRevenueEvent, original.transactionId)
                with Session(engine) as newer:
                    with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=refunded):
                        create_or_update_transaction(request, newer.get(User, "concurrent-user"), newer)
                # The old session still has unrevoked objects in its identity map.
                self.assertIsNone(old_transaction.revoked_date)
                self.assertIsNone(old_revenue.revoked_date_ms)
                with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=original):
                    create_or_update_transaction(request, old_user, older)
                older.refresh(old_transaction)
                older.refresh(old_revenue)
                self.assertEqual(old_transaction.revoked_date, refunded.revocationDate)
                self.assertEqual(old_revenue.revoked_date_ms, refunded.revocationDate)
                self.assertIsNone(old_user.premium_valid_until)
            engine.dispose()

    def test_replaying_old_transaction_does_not_replace_newer_renewal_entitlement(self):
        initial = fixtures.ExperimentReportingTests.decoded_transaction("initial")
        initial.expiresDate = 10_000_000_000_000
        renewal = fixtures.ExperimentReportingTests.decoded_transaction("renewal")
        renewal.purchaseDate += self.DAY * 7
        renewal.signedDate += self.DAY * 7
        renewal.expiresDate = initial.expiresDate + self.DAY * 365
        refund_initial = fixtures.ExperimentReportingTests.decoded_transaction("initial")
        refund_initial.signedDate = renewal.signedDate + 1_000
        refund_initial.revocationDate = refund_initial.signedDate
        request = CreateTransactionRequest(jws_payload="verified-fixture")
        with patch("core.services.revenue_measurement.calculate_premium_valid_until", side_effect=lambda value: value):
            for decoded in (initial, renewal, initial, refund_initial):
                with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
                    create_or_update_transaction(request, self.user, self.session)
            self.assertEqual(self.user.premium_valid_until, renewal.expiresDate)


class DiagnosticTests(unittest.TestCase):
    setUp = fixtures.ExperimentReportingTests.setUp
    tearDown = fixtures.ExperimentReportingTests.tearDown

    def event(self, name="paywall_viewed", **kwargs):
        return DiagnosticEvent(
            event_id=kwargs.pop("event_id", uuid4()), event_name=name,
            occurred_at_ms=current_time_ms(), installation_id=kwargs.pop("installation_id", uuid4()),
            app_version="3.7", build_number="117", analytics_environment="development",
            build_configuration="debug", paywall_presentation_id=kwargs.pop("paywall_presentation_id", uuid4()),
            **kwargs,
        )

    def send(self, *events):
        return record_diagnostic_events(DiagnosticBatch(events=list(events)), self.user, self.session)

    def test_durable_retry_is_idempotent_and_environment_report_defaults_to_production(self):
        event = self.event()
        self.assertEqual(self.send(event), {"detail": "success", "accepted": 1, "duplicates": 0})
        self.assertEqual(self.send(event), {"detail": "success", "accepted": 0, "duplicates": 1})
        prod = get_diagnostic_report(limit=100, session=self.session)
        dev = get_diagnostic_report(analytics_environment="development", limit=100, session=self.session)
        self.assertEqual(prod["count"], 0)
        self.assertEqual(dev["count"], 1)
        self.assertFalse(dev["events"][0]["server_verified_transaction"])

    def test_sensitive_extra_fields_and_invalid_checkout_context_are_rejected(self):
        for properties in ({"query": "private itinerary"}, {"reason": "email@example.com"}):
            with self.assertRaises(ValidationError):
                self.event(properties=properties)
        with self.assertRaises(ValidationError):
            self.event("checkout_attempt_completed", properties={"outcome": "verified"})
        with self.assertRaises(ValidationError):
            DiagnosticEvent.model_validate({**self.event().model_dump(), "analytics_environment": "production"})

    def test_product_load_status_is_bounded_and_presentation_scoped(self):
        event = self.event("paywall_products_loaded", properties={
            "status": "unavailable", "available_plan_count": 0, "source": "onboarding",
        })
        self.assertEqual(self.send(event)["accepted"], 1)
        with self.assertRaises(ValidationError):
            self.event("paywall_products_loaded", paywall_presentation_id=None)
        with self.assertRaises(ValidationError):
            self.event("paywall_products_loaded", properties={"available_plan_count": 21})

    def test_duplicate_terminal_is_rejected_atomically_and_facts_cannot_be_overwritten(self):
        attempt, presentation, installation = uuid4(), uuid4(), uuid4()
        common = {"checkout_attempt_id": attempt, "paywall_presentation_id": presentation, "installation_id": installation}
        start = self.event("af_initiated_checkout", **common)
        cancel = self.event("checkout_attempt_completed", properties={"outcome": "cancelled"}, **common)
        self.send(start, cancel)
        error = self.event("checkout_attempt_completed", properties={"outcome": "error"}, **common)
        with self.assertRaises(HTTPException):
            self.send(self.event(), error)
        self.assertEqual(len(self.session.exec(select(ExperimentDiagnosticEvent)).all()), 2)
        report = get_diagnostic_report(analytics_environment="development", limit=100, session=self.session)
        self.assertEqual(report["checkout_attempts"][0]["terminal_count"], 1)
        with self.assertRaises(HTTPException):
            self.send(self.event(event_id=start.event_id))

    def test_report_requires_admin_credential_and_ingest_requires_user_auth(self):
        app = FastAPI()
        app.include_router(router, prefix="/subscriptions")
        client = TestClient(app)
        self.assertIn(client.get("/subscriptions/experiments/events/report").status_code, (401, 403))
        self.assertIn(client.post("/subscriptions/experiments/events", json={"events": [self.event().model_dump(mode="json")]}).status_code, (401, 403))

    def test_authenticated_json_batch_roundtrip_uses_strict_schema_and_admin_read(self):
        app = FastAPI()
        app.include_router(router, prefix="/subscriptions")
        def test_session():
            with type(self.session)(self.engine) as session:
                yield session
        app.dependency_overrides[get_session] = test_session
        client = TestClient(app)
        event = self.event("paywall_alternative_plans_revealed", properties={
            "session_id": str(uuid4()), "visible_plan_count": 2,
        })
        token = create_jwt(sub=self.user.id)
        response = client.post("/subscriptions/experiments/events",
                               headers={"Authorization": f"Bearer {token}"},
                               json={"events": [event.model_dump(mode="json")]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], 1)
        self.assertEqual(client.get("/subscriptions/experiments/events/report",
            headers={"Authorization": f"Bearer {token}"}).status_code, 401)
        report = client.get("/subscriptions/experiments/events/report?analytics_environment=development",
            headers={"Authorization": f"Bearer {settings.LAMBDA_FUNCTION_AUTH_TOKEN}"})
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["count"], 1)

    def test_database_rejects_second_terminal_even_if_both_requests_pass_preflight(self):
        attempt = uuid4()
        first = self.event("checkout_attempt_completed", checkout_attempt_id=attempt,
                           properties={"outcome": "cancelled"})
        self.send(first)
        persisted = self.session.get(ExperimentDiagnosticEvent, str(first.event_id))
        second = ExperimentDiagnosticEvent.model_validate({
            **persisted.model_dump(), "id": str(uuid4()),
            "properties_json": '{"outcome":"verified"}',
        })
        self.session.add(second)
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()
        self.assertEqual(len(self.session.exec(select(ExperimentDiagnosticEvent)).all()), 1)

    def test_flight_recovery_correlation_is_retained_without_query_text(self):
        journey = str(uuid4())
        selected = self.event("flight_selected", properties={"flight_id": 42, "search_journey_id": journey, "search_attempt_number": 2})
        failed = self.event("flight_add_failed", installation_id=selected.installation_id,
                            properties={"flight_id": 42, "search_journey_id": journey,
                                        "search_attempt_number": 2, "reason": "backend_assign_failed"})
        self.send(selected, failed)
        report = get_diagnostic_report(analytics_environment="development", limit=100, session=self.session)
        self.assertEqual({event["properties"]["search_journey_id"] for event in report["events"]}, {journey})
        self.assertEqual(report["event_counts"]["flight_add_failed"], 1)


if __name__ == "__main__":
    unittest.main()
