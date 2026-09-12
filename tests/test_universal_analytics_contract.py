"""Synthetic schema-21 universal-flow compatibility, not launch/SDK proof.

Existing captured cohorts remain readable and retryable. A new compact paywall
is a presentation, not permission to fabricate an experiment or fallback arm.
Run with an empty inherited environment from a clean temporary directory.
"""

import json
import unittest
from uuid import uuid4
from unittest.mock import patch

from tests import test_experiment_reporting as fixtures

from pydantic import ValidationError
from sqlmodel import select

from core.activation_journey_contract import (
    ActivationJourneyAssignmentRequest,
    ActivationJourneyContext,
    ActivationJourneyEnrollmentRequest,
    canonical_context,
)
from core.config import settings
from core.models.activation_journey import (
    ActivationJourneyAssignment,
    ActivationJourneyAttribution,
    ActivationJourneyDiagnosticContext,
    ActivationJourneyEnrollment,
    ActivationJourneyIdentity,
    ActivationJourneySelection,
)
from core.models.apple_ads import AppStoreRevenueEvent
from core.models.experiment import (
    ExperimentConversion,
    ExperimentDiagnosticEvent,
    ExperimentEnrollment,
    ExperimentExposure,
    current_time_ms,
)
from core.models.transaction import Transaction
from core.models.user import User
from core.routers.activation_journey import assignment, enrollment
from core.routers.experiment_diagnostics import DiagnosticBatch, DiagnosticEvent, record_diagnostic_events
from core.routers.subscriptions import (
    CreateTransactionRequest,
    create_or_update_transaction,
    report_experiment_exposure,
)


YEARLY_PRODUCT = "com.zhirayr.Flighttracker.yearly.trial7d.4999"
COHORT_TABLES = (
    ExperimentExposure, ExperimentEnrollment, ExperimentConversion,
    ActivationJourneyAssignment, ActivationJourneyIdentity, ActivationJourneyEnrollment,
    ActivationJourneyDiagnosticContext, ActivationJourneySelection, ActivationJourneyAttribution,
)


class UniversalAnalyticsContractTests(unittest.TestCase):
    setUp = fixtures.ExperimentReportingTests.setUp
    tearDown = fixtures.ExperimentReportingTests.tearDown

    def event(self, name, installation, *, properties=None, presentation=None, attempt=None,
              experiment=None, journey=None, schema=21, occurred_at=None):
        captured = journey or experiment
        return DiagnosticEvent(
            event_id=uuid4(), event_name=name,
            occurred_at_ms=current_time_ms() if occurred_at is None else occurred_at,
            installation_id=installation,
            app_version=captured.app_version if captured else "3.9",
            build_number=captured.build_number if captured else "141",
            analytics_environment="testflight", build_configuration="release",
            paywall_presentation_id=presentation, checkout_attempt_id=attempt,
            experiment=experiment, journey=journey,
            properties={"event_schema_version": schema, **(properties or {})},
        )

    def send(self, *events):
        return record_diagnostic_events(DiagnosticBatch(events=list(events)), self.user, self.session)

    def snapshot(self, tables=COHORT_TABLES):
        self.session.expire_all()
        return {
            table.__name__: sorted(
                (row.model_dump(mode="json") for row in self.session.exec(select(table)).all()),
                key=lambda row: row["id"],
            ) for table in tables
        }

    @staticmethod
    def universal_properties():
        # Neutral machine codes describe delivery, not an assigned cohort.
        # effective_offer is intentionally absent: "universal" is not a value
        # in that historical experiment-only enum.
        return {
            "source": "universal_onboarding", "config_version": "universal_onboarding_v1",
            "layout": "compact_flight_detail",
            "effective_onboarding": "search_first", "effective_paywall": "flight_detail",
            "paywall_surface": "selected_flight", "offer_context": "selected_flight_4999",
            "assigned_product_id": YEARLY_PRODUCT, "goals_status": "not_asked",
        }

    def universal_flow(self, installation):
        presentation, attempt = uuid4(), uuid4()
        onboarding = {"source": "universal_onboarding", "effective_onboarding": "search_first"}
        paywall = self.universal_properties()
        return [
            self.event("onboarding_started", installation, properties=onboarding),
            self.event("onboarding_step_viewed", installation,
                       properties={**onboarding, "step_key": "search", "step": 1}),
            self.event("flight_selected", installation, properties={
                **onboarding, "flight_identity": "a" * 64, "selection_stage": "result_tap",
            }),
            self.event("paywall_viewed", installation, presentation=presentation, properties=paywall),
            self.event("paywall_products_loaded", installation, presentation=presentation,
                       properties={**paywall, "available_plan_count": 2}),
            self.event("subscription_product_selected", installation, presentation=presentation,
                       properties={**paywall, "product_id": YEARLY_PRODUCT,
                                   "displayed_product_id": YEARLY_PRODUCT, "selection_method": "default"}),
            self.event("af_initiated_checkout", installation, presentation=presentation, attempt=attempt,
                       properties={**paywall, "product_id": YEARLY_PRODUCT}),
            self.event("checkout_attempt_completed", installation, presentation=presentation, attempt=attempt,
                       properties={**paywall, "product_id": YEARLY_PRODUCT, "outcome": "pending"}),
            self.event("onboarding_completed", installation, properties=onboarding),
        ]

    def assert_cohortless(self, events):
        for event in events:
            row = self.session.get(ExperimentDiagnosticEvent, str(event.event_id), populate_existing=True)
            self.assertIsNotNone(row)
            self.assertIsNone(row.experiment_id)
            self.assertIsNone(row.variant)
            self.assertIsNone(row.measurement_revision)
            self.assertIsNone(self.session.get(ActivationJourneyDiagnosticContext, row.id))
            properties = json.loads(row.properties_json)
            self.assertNotIn("_activation_journey", properties)
            self.assertNotIn("effective_offer", properties)
            self.assertNotIn("assignment_source", properties)
            self.assertEqual(properties["event_schema_version"], 21)

    def historical_legacy(self):
        installation = uuid4()
        context = fixtures.ExperimentReportingTests.context().model_copy(update={
            "installation_id": installation,
            "exposure_id": f"activation_experience_2026_08:{installation}",
            "analytics_environment": "testflight",
            "exposed_at_ms": current_time_ms() - 3_600_000,
        })
        report_experiment_exposure(context, self.user, self.session)
        return context

    def historical_journey(self):
        request = ActivationJourneyAssignmentRequest(
            installation_id=uuid4(), enrollment_event_id=uuid4(),
            enrolled_at_ms=current_time_ms() - 3_600_000,
            app_version="3.8", build_number="130", analytics_environment="testflight",
            is_new_installation=True,
        )
        # Explicitly construct a previously captured fixture before universal
        # events; these mocked flags are not exercised by the universal path.
        with patch.object(settings, "ACTIVATION_JOURNEY_NONPRODUCTION_ENROLLMENT_ENABLED", True), \
                patch.object(settings, "ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT", 0), \
                patch.object(settings, "ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT", "search_first_flight_detail"), \
                patch("core.services.activation_journey.current_time_ms", return_value=request.enrolled_at_ms + 1000):
            context = assignment(request, self.user, self.session)["journey"]
            enrollment(ActivationJourneyEnrollmentRequest(journey=context), self.user, self.session)
        return context

    def verified_payload(self, transaction_id, *, purchase_at=None):
        decoded = fixtures.ExperimentReportingTests.decoded_transaction(transaction_id)
        decoded.originalTransactionId = "original-" + transaction_id
        decoded.productId = YEARLY_PRODUCT
        decoded.purchaseDate = purchase_at or current_time_ms() - 1000
        decoded.originalPurchaseDate = decoded.purchaseDate
        decoded.signedDate = decoded.purchaseDate + 100
        decoded.expiresDate = decoded.purchaseDate + 7 * 86_400_000
        decoded.environment = fixtures.Environment.SANDBOX
        decoded.appAccountToken = self.user.id
        decoded.price = 49_990
        decoded.offerDiscountType = None
        decoded.offerPeriod = None
        return decoded

    def register_transaction(self, decoded, *, experiment=None, journey=None, user=None):
        request = CreateTransactionRequest(jws_payload="synthetic-already-verified-jws",
                                           experiment=experiment, journey=journey)
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
            return create_or_update_transaction(request, user or self.user, self.session)

    def test_schema21_universal_flow_is_cohortless_and_exact_retry_is_idempotent(self):
        events = self.universal_flow(uuid4())
        self.assertEqual(self.send(*events)["accepted"], len(events))
        self.assertEqual(self.send(*events)["duplicates"], len(events))
        self.assert_cohortless(events)
        self.assertTrue(all(rows == [] for rows in self.snapshot().values()))
        self.assertEqual(len(self.session.exec(select(ExperimentDiagnosticEvent)).all()), len(events))
        paywall = self.session.get(ExperimentDiagnosticEvent, str(events[3].event_id))
        self.assertEqual(json.loads(paywall.properties_json),
                         {"event_schema_version": 21, **self.universal_properties()})

    def test_same_installation_legacy_history_is_unchanged_by_universal_capture(self):
        context = self.historical_legacy()
        old = self.event("onboarding_started", context.installation_id, experiment=context, schema=19,
                         occurred_at=context.exposed_at_ms + 1000)
        self.send(old)
        original = self.snapshot()
        original_event = self.session.get(ExperimentDiagnosticEvent, str(old.event_id)).model_dump()
        events = self.universal_flow(context.installation_id)
        self.send(*events)
        self.assertEqual(self.send(old, *events)["duplicates"], len(events) + 1)
        self.assert_cohortless(events)
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(self.session.get(ExperimentDiagnosticEvent, str(old.event_id)).model_dump(), original_event)

    def test_same_installation_journey_history_is_unchanged_by_universal_capture(self):
        context = self.historical_journey()
        old = self.event("onboarding_started", context.installation_id, journey=context, schema=18,
                         occurred_at=context.enrolled_at_ms + 1000)
        self.send(old)
        original = self.snapshot()
        original_event = self.session.get(ExperimentDiagnosticEvent, str(old.event_id)).model_dump()
        events = self.universal_flow(context.installation_id)
        self.send(*events)
        self.assertEqual(self.send(old, *events)["duplicates"], len(events) + 1)
        self.assert_cohortless(events)
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(self.session.get(ExperimentDiagnosticEvent, str(old.event_id)).model_dump(), original_event)
        sidecar = self.session.get(ActivationJourneyDiagnosticContext, str(old.event_id))
        self.assertEqual(sidecar.context_json, canonical_context(context))

    def test_presentation_and_attempt_ids_remain_required_without_a_cohort(self):
        for name in ("paywall_viewed", "paywall_dismissed", "paywall_products_loaded",
                     "subscription_product_selected", "paywall_alternative_plans_revealed",
                     "af_initiated_checkout", "checkout_attempt_completed"):
            properties = {**self.universal_properties(), "outcome": "pending"}
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.event(name, uuid4(), attempt=uuid4(), properties=properties)
        for name in ("af_initiated_checkout", "checkout_attempt_completed"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.event(name, uuid4(), presentation=uuid4(), properties={"outcome": "pending"})

    def test_compact_flow_does_not_invent_journey_fallback_or_effective_offer_values(self):
        for name in ("activation_journey_enrolled", "activation_journey_selected_flight"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.event(name, uuid4(), properties={"selection_eligible": True, "flight_identity": "a" * 64})
        with self.assertRaises(ValidationError):
            self.event("paywall_viewed", uuid4(), presentation=uuid4(), properties={
                **self.universal_properties(), "effective_offer": "universal",
            })
        installation = uuid4()
        with self.assertRaises(ValidationError):
            ActivationJourneyContext(
                experiment_id="activation_journey_2026_09", measurement_revision=1,
                variant="search_first_flight_detail", eligible=False, randomized=False,
                installation_id=installation, exposure_id=f"activation_journey_2026_09:{installation}",
                enrollment_event_id=uuid4(), enrolled_at_ms=current_time_ms(),
                app_version="3.9", build_number="141", analytics_environment="testflight",
                assignment_source="configuration_fallback", config_version="universal_onboarding_v1",
                intended_onboarding="search_first", intended_paywall="flight_detail", goals_status="not_asked",
            )
        self.assertTrue(all(rows == [] for rows in self.snapshot().values()))

    def test_verified_universal_transaction_commits_revenue_without_any_cohort(self):
        self.send(*self.universal_flow(uuid4()))
        decoded = self.verified_payload("universal-paid")
        self.assertEqual(self.register_transaction(decoded), {"detail": "successfull"})
        self.assertEqual(self.register_transaction(decoded), {"detail": "successfull"})
        self.assertEqual(len(self.session.exec(select(Transaction)).all()), 1)
        facts = self.session.exec(select(AppStoreRevenueEvent)).all()
        self.assertEqual(len(facts), 1)
        self.assertEqual((facts[0].product_id, facts[0].price_milliunits, facts[0].purchase_environment),
                         (YEARLY_PRODUCT, 49_990, "Sandbox"))
        self.assertFalse(facts[0].starts_trial)
        self.assertTrue(all(rows == [] for rows in self.snapshot().values()))

    def test_old_pending_legacy_metadata_retries_after_universal_capture_without_reassignment(self):
        context = self.historical_legacy()
        exposure_before = self.snapshot((ExperimentExposure,))
        decoded = self.verified_payload("old-legacy-paid", purchase_at=context.exposed_at_ms + 10_000)
        with patch("core.routers.subscriptions._record_experiment_conversion", side_effect=RuntimeError("synthetic storage failure")), \
                self.assertLogs("core.routers.subscriptions", level="ERROR"):
            self.assertEqual(self.register_transaction(decoded, experiment=context)["experiment_tracking_status"], "pending")
        self.assertIsNotNone(self.session.get(AppStoreRevenueEvent, decoded.transactionId))
        self.assertEqual(self.session.exec(select(ExperimentConversion)).all(), [])
        self.send(*self.universal_flow(context.installation_id))
        self.assertEqual(self.register_transaction(decoded, experiment=context), {"detail": "successfull"})
        conversion = self.session.get(ExperimentConversion, decoded.transactionId)
        self.assertEqual((conversion.experiment_id, conversion.variant, conversion.installation_id,
                          conversion.exposed_at_ms, conversion.user_id, conversion.analytics_environment),
                         (context.experiment_id, context.variant, str(context.installation_id),
                          context.exposed_at_ms, self.user.id, context.analytics_environment))
        original = self.snapshot()
        self.assertEqual(self.register_transaction(decoded), {"detail": "successfull"})
        self.assertEqual(self.register_transaction(decoded, experiment=context), {"detail": "successfull"})
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(self.snapshot((ExperimentExposure,)), exposure_before)
        self.assertEqual(len(self.session.exec(select(AppStoreRevenueEvent)).all()), 1)

    def test_old_pending_journey_metadata_preserves_exact_context_and_owner(self):
        context = self.historical_journey()
        decoded = self.verified_payload("old-journey-paid", purchase_at=context.enrolled_at_ms + 10_000)
        with patch("core.routers.subscriptions.attribute_journey_transaction", side_effect=RuntimeError("synthetic storage failure")), \
                self.assertLogs("core.routers.subscriptions", level="WARNING"):
            self.assertEqual(self.register_transaction(decoded, journey=context)["experiment_tracking_status"], "pending")
        self.assertIsNotNone(self.session.get(AppStoreRevenueEvent, decoded.transactionId))
        self.assertEqual(self.session.exec(select(ActivationJourneyAttribution)).all(), [])
        events = self.universal_flow(context.installation_id)
        self.send(*events)
        self.assertEqual(self.register_transaction(decoded, journey=context), {"detail": "successfull"})
        attribution = self.session.get(ActivationJourneyAttribution, decoded.originalTransactionId)
        self.assertEqual(attribution.context_json, canonical_context(context))
        self.assertEqual(attribution.user_id, self.user.id)
        original = self.snapshot()
        self.assertEqual(self.register_transaction(decoded), {"detail": "successfull"})
        self.assertEqual(self.register_transaction(decoded, journey=context), {"detail": "successfull"})
        other = User(id="other-universal-account")
        self.session.add(other)
        self.session.commit()
        result = self.register_transaction(decoded, journey=context, user=other)
        self.assertEqual(result, {"detail": "successfull", "experiment_tracking_status": "conflict"})
        self.assertEqual(self.snapshot(), original)
        self.assert_cohortless(events)
        self.assertEqual(len(self.session.exec(select(AppStoreRevenueEvent)).all()), 1)
        self.assertEqual(self.session.exec(select(ExperimentConversion)).all(), [])


if __name__ == "__main__":
    unittest.main()
