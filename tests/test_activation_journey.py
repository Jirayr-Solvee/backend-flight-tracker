"""Isolated new-protocol contracts; never use a production database or provider."""

import json
import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4
from unittest.mock import patch

from tests import test_experiment_reporting as legacy

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, SQLModel, create_engine, select

from core.activation_journey_contract import ActivationJourneyAssignmentRequest, ActivationJourneyContext, ActivationJourneyEnrollmentRequest, JOURNEY_SCOPES, JOURNEY_VARIANTS
from core.config import Settings, settings
from core.dependency import get_current_user
from core.models import get_session
from core.models.activation_journey import (
    ActivationJourneyAssignment, ActivationJourneyAttribution, ActivationJourneyDiagnosticContext,
    ActivationJourneyEnrollment, ActivationJourneyGoalReceipt, ActivationJourneyGoalSelection, ActivationJourneyIdentity, ActivationJourneySelection,
)
from core.models.apple_ads import AppStoreRevenueEvent
from core.models.experiment import ExperimentConversion, ExperimentDiagnosticEvent, ExperimentExposure, ExperimentGoalConfirmation, ExperimentGoalConfirmationReceipt, current_time_ms
from core.models.transaction import Transaction
from core.models.user import User
from core.routers.activation_journey import assignment, enrollment, router as journey_router
from core.routers.experiment_diagnostics import DiagnosticBatch, DiagnosticEvent, record_diagnostic_events
from core.routers.subscriptions import CreateTransactionRequest, ExperimentGoalSelectionRequest, ExperimentEnrollmentRequest, create_or_update_transaction, report_experiment_goal_selection, report_experiment_enrollment, router as subscriptions_router
from core.services.activation_journey_reporting import baseline_manifest, journey_summary

DAY = 86_400_000


class ActivationJourneyTests(unittest.TestCase):
    setUp = legacy.ExperimentReportingTests.setUp
    tearDown = legacy.ExperimentReportingTests.tearDown

    def request(self, *, environment="production", enrolled=None, installation=None):
        return ActivationJourneyAssignmentRequest(installation_id=installation or uuid4(), enrollment_event_id=uuid4(),
            enrolled_at_ms=enrolled if enrolled is not None else current_time_ms() - 3600_000,
            app_version="3.9", build_number="120", analytics_environment=environment, is_new_installation=True)

    def context(self, variant="search_first_standard", *, environment="production", enrolled=None, do_enroll=True):
        request = self.request(environment=environment, enrolled=enrolled)
        with patch.object(settings, "ACTIVATION_JOURNEY_PRODUCTION_ENROLLMENT_ENABLED", True), \
             patch.object(settings, "ACTIVATION_JOURNEY_NONPRODUCTION_ENROLLMENT_ENABLED", True), \
             patch.object(settings, "ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT", 100 if variant == "search_first_standard" else 0), \
             patch.object(settings, "ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT", "search_first_flight_detail" if variant == "search_first_flight_detail" else "goals_flight_detail"), \
             patch("core.services.activation_journey.current_time_ms", return_value=request.enrolled_at_ms + 1000):
            context = assignment(request, self.user, self.session)["journey"]
            if do_enroll:
                enrollment(ActivationJourneyEnrollmentRequest(journey=context), self.user, self.session)
        return context

    def event(self, context, name="af_search", *, offset=1000, identity=None, properties=None, attempt=None, presentation=None):
        return DiagnosticEvent(event_id=identity or uuid4(), event_name=name,
            occurred_at_ms=context.enrolled_at_ms + offset, installation_id=context.installation_id,
            app_version=context.app_version, build_number=context.build_number,
            analytics_environment=context.analytics_environment,
            build_configuration="debug" if context.analytics_environment == "development" else "release",
            journey=context, properties={"event_schema_version": 18, **(properties or {})},
            checkout_attempt_id=attempt,
            paywall_presentation_id=presentation or (uuid4() if name.startswith("paywall_") or name == "subscription_product_selected" else None))

    def send(self, *events, user=None):
        return record_diagnostic_events(DiagnosticBatch(events=list(events)), user or self.user, self.session)

    def purchase(self, context, *, purchase_at=None, trial=True, transaction="transaction-journey", original=None, journey_override=None, verified_owner=None):
        decoded = legacy.ExperimentReportingTests.decoded_transaction(transaction)
        decoded.originalTransactionId = original or f"original-{context.installation_id}"
        decoded.purchaseDate = purchase_at or context.enrolled_at_ms + 10_000
        decoded.originalPurchaseDate = context.enrolled_at_ms + 10_000
        decoded.signedDate = decoded.purchaseDate + 100
        decoded.expiresDate = decoded.purchaseDate + 7 * DAY
        decoded.price = 0 if trial else 9990
        decoded.offerDiscountType = legacy.OfferDiscountType.FREE_TRIAL if trial else None
        decoded.appAccountToken = verified_owner or self.user.id
        if context.analytics_environment != "production":
            decoded.environment = legacy.Environment.SANDBOX
        request = CreateTransactionRequest(jws_payload="synthetic-signed-jws", journey=context if journey_override is None else journey_override)
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
            return create_or_update_transaction(request, self.user, self.session)

    def checkout_proof(self, context, identity="a" * 64, surface="selected_flight", transaction="transaction-journey"):
        fact = self.session.get(AppStoreRevenueEvent, transaction)
        attempt, presentation = uuid4(), uuid4()
        properties = {"product_id": fact.product_id, "paywall_surface": surface,
                      "effective_paywall": "standard", "effective_offer": "standard"}
        if identity is not None:
            properties["flight_identity"] = identity
        self.send(self.event(context, "af_initiated_checkout", offset=9000, attempt=attempt, presentation=presentation, properties=properties),
                  self.event(context, "af_start_trial" if fact.starts_trial else "af_purchase", offset=11000, attempt=attempt, presentation=presentation,
                             properties={**properties, "transaction_id": fact.id, "purchase_environment": fact.purchase_environment}))

    def test_default_off_and_sticky_server_proposal(self):
        request = self.request()
        first = assignment(request, self.user, self.session)["journey"]
        self.assertFalse(first.eligible)
        self.assertFalse(first.randomized)
        self.assertEqual(first.assignment_source, "server_disabled")
        self.assertEqual(first.variant, "search_first_standard")
        with patch.object(settings, "ACTIVATION_JOURNEY_PRODUCTION_ENROLLMENT_ENABLED", True), patch.object(settings, "ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT", 0):
            self.assertEqual(assignment(request, self.user, self.session)["journey"], first)
        self.assertEqual(self.session.exec(select(ActivationJourneyEnrollment)).all(), [])

    def test_all_three_variant_scopes_remain_distinct_and_strict(self):
        expected = {
            "search_first_standard": ("search_first", "standard", "not_asked"),
            "goals_flight_detail": ("goals", "flight_detail", "required"),
            "search_first_flight_detail": ("search_first", "flight_detail", "not_asked"),
        }
        self.assertEqual(set(JOURNEY_VARIANTS), set(expected))
        self.assertEqual(JOURNEY_SCOPES, expected)
        for variant, scope in expected.items():
            with self.subTest(variant=variant):
                context = self.context(variant)
                self.assertEqual((context.intended_onboarding, context.intended_paywall, context.goals_status), scope)
                for other, other_scope in expected.items():
                    if other == variant:
                        continue
                    with self.assertRaises(ValidationError):
                        ActivationJourneyContext.model_validate({**context.model_dump(),
                            "intended_onboarding": other_scope[0], "intended_paywall": other_scope[1], "goals_status": other_scope[2]})

    def test_candidate_selector_is_explicit_and_never_enables_either_environment(self):
        self.assertFalse(settings.ACTIVATION_JOURNEY_PRODUCTION_ENROLLMENT_ENABLED)
        self.assertFalse(settings.ACTIVATION_JOURNEY_NONPRODUCTION_ENROLLMENT_ENABLED)
        self.assertEqual(settings.ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT, "goals_flight_detail")
        selector = TypeAdapter(Settings.model_fields["ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT"].annotation)
        for valid in ("goals_flight_detail", "search_first_flight_detail"):
            self.assertEqual(selector.validate_python(valid), valid)
        for invalid in ("search_first_standard", "invented", "", None):
            with self.assertRaises(ValidationError):
                selector.validate_python(invalid)
        with patch.object(settings, "ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT", "search_first_flight_detail"), \
             patch.object(settings, "ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT", 0):
            for environment in ("production", "testflight", "development"):
                context = assignment(self.request(environment=environment), self.user, self.session)["journey"]
                self.assertEqual(context.variant, "search_first_standard")
                self.assertFalse(context.eligible)
                self.assertFalse(context.randomized)
                self.assertEqual(context.assignment_source, "server_disabled")

    def test_candidate_allocation_reuses_standard_bucket_without_remapping_old_arm(self):
        from uuid import UUID
        installations = {}
        for number in range(1, 100):
            installation = UUID(int=number)
            bucket = int.from_bytes(hashlib.sha256(f"activation_journey_2026_09:{installation}".encode()).digest()[:8], "big") % 100
            installations.setdefault(bucket < 50, installation)
            if len(installations) == 2:
                break
        with patch.object(settings, "ACTIVATION_JOURNEY_PRODUCTION_ENROLLMENT_ENABLED", True), \
             patch.object(settings, "ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT", 50), \
             patch.object(settings, "ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT", "search_first_flight_detail"), \
             patch.object(settings, "ACTIVATION_JOURNEY_CONFIG_VERSION", "candidate_2026_09_11"):
            for standard_bucket, installation in installations.items():
                context = assignment(self.request(installation=installation), self.user, self.session)["journey"]
                self.assertEqual(context.variant, "search_first_standard" if standard_bucket else "search_first_flight_detail")
                self.assertEqual(context.intended_onboarding, "search_first")
                self.assertEqual(context.goals_status, "not_asked")
                self.assertEqual(context.config_version, "candidate_2026_09_11")
        self.assertEqual(len(installations), 2)

    def test_allocation_change_preserves_proposals_and_enrollments_in_both_directions(self):
        for variant in JOURNEY_VARIANTS:
            for enrolled in (False, True):
                with self.subTest(variant=variant, enrolled=enrolled):
                    original = self.context(variant, do_enroll=enrolled)
                    row = self.session.get(ActivationJourneyAssignment, original.exposure_id + ":v1")
                    original_bytes = row.context_json
                    request = ActivationJourneyAssignmentRequest.model_validate_json(row.request_json)
                    with patch.object(settings, "ACTIVATION_JOURNEY_PRODUCTION_ENROLLMENT_ENABLED", True), \
                         patch.object(settings, "ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT", 0), \
                         patch.object(settings, "ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT", "goals_flight_detail" if variant == "search_first_flight_detail" else "search_first_flight_detail"), \
                         patch.object(settings, "ACTIVATION_JOURNEY_CONFIG_VERSION", "different_future_config"):
                        self.assertEqual(assignment(request, self.user, self.session)["journey"], original)
                        enrollment(ActivationJourneyEnrollmentRequest(journey=original), self.user, self.session)
                    self.assertEqual(self.session.get(ActivationJourneyAssignment, row.id).context_json, original_bytes)
                    self.assertEqual(self.session.get(ActivationJourneyEnrollment, row.id).context_json, original_bytes)

    def test_candidate_enrollment_is_idempotent_and_does_not_create_legacy_cohorts(self):
        context = self.context("search_first_flight_detail", do_enroll=False)
        event = self.event(context, "activation_journey_enrolled", offset=0, identity=context.enrollment_event_id,
                           properties={"goals_status": "not_asked", "effective_onboarding": "search_first"})
        self.assertEqual(self.send(event)["accepted"], 1)
        self.assertEqual(self.send(event)["duplicates"], 1)
        enrollment(ActivationJourneyEnrollmentRequest(journey=context), self.user, self.session)
        row = self.session.exec(select(ActivationJourneyEnrollment)).one()
        self.assertEqual(row.variant, "search_first_flight_detail")
        self.assertEqual(row.enrollment_event_id, str(context.enrollment_event_id))
        self.assertEqual(self.session.exec(select(ExperimentExposure)).all(), [])
        self.assertEqual(self.session.exec(select(ExperimentConversion)).all(), [])

    def test_candidate_assignment_enrollment_and_goals_through_http(self):
        app = FastAPI()
        for router in (journey_router, subscriptions_router):
            app.include_router(router, prefix="/subscriptions")
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_session] = lambda: self.session
        client = TestClient(app)
        with patch.object(settings, "ACTIVATION_JOURNEY_PRODUCTION_ENROLLMENT_ENABLED", True), \
             patch.object(settings, "ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT", 0), \
             patch.object(settings, "ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT", "search_first_flight_detail"):
            response = client.post("/subscriptions/activation-journey/assignment", json=self.request().model_dump(mode="json"))
        self.assertEqual(response.status_code, 200)
        context = response.json()["journey"]
        self.assertEqual(context["variant"], "search_first_flight_detail")
        for _ in range(2):
            response = client.post("/subscriptions/activation-journey/enrollment", json={"journey": context})
            self.assertEqual(response.status_code, 200)
        response = client.post("/subscriptions/experiments/goals", json={
            "journey": context, "selected_goal_keys": ["family_friends"],
            "selected_at_ms": context["enrolled_at_ms"] + 1000,
            "confirmation_revision": 1, "confirmation_id": str(uuid4()),
        })
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "Goals were not asked in this journey")
        self.assertEqual(len(self.session.exec(select(ActivationJourneyEnrollment)).all()), 1)
        self.assertEqual(self.session.exec(select(ActivationJourneyGoalSelection)).all(), [])

    def test_candidate_rejects_goals_and_false_confirmation_diagnostics(self):
        for variant in ("search_first_standard", "search_first_flight_detail"):
            context = self.context(variant)
            request = ExperimentGoalSelectionRequest(journey=context, selected_goal_keys=["family_friends"],
                selected_at_ms=context.enrolled_at_ms + 1000, confirmation_revision=1, confirmation_id=uuid4())
            with self.assertRaises(HTTPException) as failure:
                report_experiment_goal_selection(request, self.user, self.session)
            self.assertEqual(failure.exception.status_code, 422)
            self.assertEqual(self.session.exec(select(ActivationJourneyGoalSelection)).all(), [])
            self.assertEqual(self.session.exec(select(ActivationJourneyGoalReceipt)).all(), [])
            for false_status in ("required", "confirmed"):
                with self.assertRaises(ValidationError):
                    self.event(context, properties={"goals_status": false_status})
            self.send(self.event(context, properties={"goals_status": "not_asked"}))

    def test_candidate_selected_skip_and_forced_standard_delivery_preserve_assignment(self):
        context = self.context("search_first_flight_detail")
        for surface, layout, offer, override in (
            ("selected_flight", "flight_detail", "flight_detail_treatment", "none"),
            ("skip_flight", "standard", "standard", "none"),
            ("selected_flight", "standard", "standard", "forced_standard"),
        ):
            self.send(self.event(context, "paywall_viewed", properties={
                "paywall_surface": surface, "effective_onboarding": "search_first", "effective_paywall": layout,
                "effective_offer": offer, "goals_status": "not_asked", "operational_override": override,
            }))
        for invalid in ({"effective_paywall": "flight_detail", "effective_offer": "standard"},
                        {"effective_paywall": "standard", "effective_offer": "flight_detail_treatment"}):
            with self.assertRaises(ValidationError):
                self.event(context, "paywall_viewed", properties={"paywall_surface": "skip_flight", **invalid})
        for row in self.session.exec(select(ExperimentDiagnosticEvent)).all():
            frozen = json.loads(row.properties_json)["_activation_journey"]
            self.assertEqual(frozen, context.model_dump(mode="json"))
        self.assertEqual(self.session.exec(select(ActivationJourneyEnrollment)).one().variant, "search_first_flight_detail")

    def test_candidate_preview_and_fallback_keep_existing_safety_rules(self):
        context = self.context("search_first_flight_detail", environment="development", do_enroll=False)
        preview = ActivationJourneyContext.model_validate({**context.model_dump(), "eligible": False,
            "randomized": False, "assignment_source": "local_debug_preview"})
        enrollment(ActivationJourneyEnrollmentRequest(journey=preview), self.user, self.session)
        self.assertEqual(sum(arm["enrolled_installations"] for arm in journey_summary(session=self.session, analytics_environment="development")["arms"]), 0)
        for invalid in ({"analytics_environment": "production"}, {"assignment_source": "configuration_fallback"},
                        {"assignment_source": "server_disabled"}):
            with self.assertRaises(ValidationError):
                ActivationJourneyContext.model_validate({**preview.model_dump(), **invalid})

    def test_candidate_report_and_verified_transaction_are_not_pooled_with_existing_arms(self):
        now = current_time_ms()
        for variant in JOURNEY_VARIANTS:
            self.context(variant, enrolled=now - 2 * DAY)
        candidate = self.context("search_first_flight_detail", enrolled=now - 2 * DAY)
        self.send(self.event(candidate, "activation_journey_selected_flight", properties={"selection_eligible": True, "flight_identity": "c" * 64}))
        self.assertEqual(self.purchase(candidate), {"detail": "successfull"})
        report = journey_summary(session=self.session)
        arms = {arm["variant"]: arm for arm in report["arms"]}
        self.assertEqual({key: arm["enrolled_installations"] for key, arm in arms.items()}, {
            "search_first_standard": 1, "goals_flight_detail": 1, "search_first_flight_detail": 2})
        self.assertEqual({key: arm["goals"]["not_asked"] for key, arm in arms.items()}, {
            "search_first_standard": 1, "goals_flight_detail": 0, "search_first_flight_detail": 2})
        self.assertEqual(arms["search_first_flight_detail"]["verified_trial_24h"]["numerator"], 1)
        self.assertEqual(arms["goals_flight_detail"]["verified_trial_24h"]["numerator"], 0)
        self.assertEqual(report["absolute_percentage_point_differences_search_first_minus_goals"]["first_valid_selection_10m"], 0)
        self.assertEqual(report["absolute_percentage_point_differences_search_first_flight_detail_minus_search_first_standard"]["first_valid_selection_10m"], 50)
        self.assertEqual(self.session.exec(select(ExperimentConversion)).all(), [])
        self.assertEqual(self.session.exec(select(ActivationJourneyAttribution)).one().enrollment_id, candidate.exposure_id + ":v1")

    def test_candidate_attribution_normalizes_uuid_owner_without_accepting_another_owner(self):
        self.user = User(id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.session.add(self.user)
        self.session.commit()
        context = self.context("search_first_flight_detail")
        result = self.purchase(context, verified_owner=self.user.id.upper())
        self.assertEqual(result, {"detail": "successfull"})
        association = self.session.exec(select(ActivationJourneyAttribution)).one()
        self.assertEqual(association.user_id, self.user.id)
        other_owner = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        result = self.purchase(context, transaction="other-owner-transaction", original="other-owner-original", verified_owner=other_owner)
        self.assertEqual(result, {"detail": "successfull", "experiment_tracking_status": "conflict"})
        self.assertIsNotNone(self.session.get(AppStoreRevenueEvent, "other-owner-transaction"))
        self.assertEqual(len(self.session.exec(select(ActivationJourneyAttribution)).all()), 1)

    def test_proposal_replay_capture_change_and_other_account_rejected(self):
        request = self.request()
        assignment(request, self.user, self.session)
        with self.assertRaises(HTTPException) as changed:
            assignment(request.model_copy(update={"build_number": "121"}), self.user, self.session)
        self.assertEqual(changed.exception.status_code, 409)
        other = User(id="another-user")
        self.session.add(other)
        self.session.commit()
        with self.assertRaises(HTTPException) as foreign:
            assignment(request, other, self.session)
        self.assertEqual(foreign.exception.status_code, 403)

    def test_canonical_enrollment_exact_retry_and_duplicate_event_identity(self):
        context = self.context(do_enroll=False)
        event = self.event(context, "activation_journey_enrolled", offset=0, identity=context.enrollment_event_id)
        self.assertEqual(self.send(event)["accepted"], 1)
        self.assertEqual(self.send(event)["duplicates"], 1)
        enrollment(ActivationJourneyEnrollmentRequest(journey=context), self.user, self.session)
        self.assertEqual(len(self.session.exec(select(ActivationJourneyEnrollment)).all()), 1)
        self.assertEqual(self.session.exec(select(ExperimentExposure)).all(), [])
        with self.assertRaises(ValidationError):
            self.event(context, "activation_journey_enrolled", offset=1, identity=context.enrollment_event_id)

    def test_existing_legacy_installation_cannot_be_assigned(self):
        old = legacy.ExperimentReportingTests.context()
        legacy.report_experiment_exposure(old, self.user, self.session)
        with self.assertRaises(HTTPException) as failure:
            assignment(self.request(installation=old.installation_id), self.user, self.session)
        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(self.session.exec(select(ActivationJourneyAssignment)).all(), [])

    def test_timeout_fallback_wins_late_proposal_without_randomized_relabel(self):
        proposal = self.context("goals_flight_detail", do_enroll=False)
        fallback = ActivationJourneyContext.model_validate({**proposal.model_dump(), "variant": "search_first_standard",
            "eligible": False, "randomized": False, "assignment_source": "configuration_fallback",
            "intended_onboarding": "search_first", "intended_paywall": "standard", "goals_status": "not_asked"})
        self.send(self.event(fallback))
        enrollment(ActivationJourneyEnrollmentRequest(journey=fallback), self.user, self.session)
        with self.assertRaises(HTTPException) as late:
            enrollment(ActivationJourneyEnrollmentRequest(journey=proposal), self.user, self.session)
        self.assertEqual(late.exception.status_code, 409)
        summary = journey_summary(session=self.session)
        self.assertEqual(sum(arm["enrolled_installations"] for arm in summary["arms"]), 0)
        self.assertEqual(summary["quality"]["nonrandomized_entries_excluded"][0]["installations"], 1)

    def test_preview_is_nonrandomized_development_only(self):
        context = self.context("goals_flight_detail", environment="development", do_enroll=False)
        preview = ActivationJourneyContext.model_validate({**context.model_dump(), "eligible": False, "randomized": False, "assignment_source": "local_debug_preview"})
        enrollment(ActivationJourneyEnrollmentRequest(journey=preview), self.user, self.session)
        with self.assertRaises(ValidationError):
            ActivationJourneyContext.model_validate({**preview.model_dump(), "eligible": True})
        with self.assertRaises(ValidationError):
            ActivationJourneyContext.model_validate({**preview.model_dump(), "analytics_environment": "production"})

    def test_diagnostics_before_enrollment_remain_unjoined(self):
        context = self.context(do_enroll=False)
        self.send(self.event(context))
        report = journey_summary(session=self.session)
        self.assertEqual(report["quality"]["unjoined_diagnostic_events_environment_wide"], 1)
        self.assertEqual(sum(arm["enrolled_installations"] for arm in report["arms"]), 0)
        enrollment(ActivationJourneyEnrollmentRequest(journey=context), self.user, self.session)
        self.assertEqual(journey_summary(session=self.session)["quality"]["unjoined_diagnostic_events_environment_wide"], 0)

    def test_batch_conflicting_first_selection_rolls_back_all_metadata(self):
        context = self.context()
        first = self.event(context, "activation_journey_selected_flight", properties={"flight_identity": "a" * 64, "selection_eligible": True})
        second = self.event(context, "activation_journey_selected_flight", properties={"flight_identity": "b" * 64, "selection_eligible": True})
        with self.assertRaises(HTTPException):
            self.send(first, second)
        self.assertEqual(self.session.exec(select(ActivationJourneySelection)).all(), [])
        self.assertEqual(self.session.exec(select(ActivationJourneyDiagnosticContext)).all(), [])
        self.assertEqual(self.session.exec(select(ExperimentDiagnosticEvent)).all(), [])

    def test_schema_context_and_standard_skip_offer_are_strict(self):
        context = self.context()
        for changed in ({"properties": {"event_schema_version": 17}}, {"analytics_environment": "development"}, {"experiment": legacy.ExperimentReportingTests.context()}):
            with self.assertRaises(ValidationError):
                DiagnosticEvent.model_validate({**self.event(context).model_dump(), **changed})
        with self.assertRaises(ValidationError):
            self.event(context, "paywall_viewed", properties={"paywall_surface": "skip_flight", "effective_paywall": "flight_detail", "effective_offer": "flight_detail_treatment"})
        self.send(self.event(context, "paywall_viewed", properties={"paywall_surface": "skip_flight", "effective_paywall": "standard", "effective_offer": "standard"}))

    def test_first_eligible_selection_is_one_immutable_fact(self):
        context = self.context()
        event = self.event(context, "activation_journey_selected_flight", properties={"flight_identity": "a" * 64, "selection_eligible": True})
        self.send(event)
        self.send(event)
        with self.assertRaises(HTTPException):
            self.send(self.event(context, "activation_journey_selected_flight", properties={"flight_identity": "a" * 64, "selection_eligible": True}))
        self.assertEqual(len(self.session.exec(select(ActivationJourneySelection)).all()), 1)

    def test_goals_not_asked_rejected_and_monotone_confirmed_answers(self):
        search = self.context()
        def request(context, revision=1, keys=None):
            return ExperimentGoalSelectionRequest(journey=context, selected_goal_keys=keys or ["family_friends"],
                selected_at_ms=context.enrolled_at_ms + 1000, confirmation_revision=revision, confirmation_id=uuid4())
        with self.assertRaises(HTTPException):
            report_experiment_goal_selection(request(search), self.user, self.session)
        context = self.context("goals_flight_detail")
        first, newest, stale = request(context), request(context, 3, ["flight_history"]), request(context, 2)
        for item in (first, newest):
            self.assertEqual(report_experiment_goal_selection(item, self.user, self.session)["status"], "accepted")
        self.assertEqual(report_experiment_goal_selection(newest, self.user, self.session)["status"], "idempotent")
        self.assertEqual(report_experiment_goal_selection(stale, self.user, self.session)["status"], "stale")
        self.assertEqual(self.session.exec(select(ActivationJourneyGoalSelection)).one().selected_goal_keys, "flight_history")
        with self.assertRaises(HTTPException):
            report_experiment_goal_selection(request(context, 3), self.user, self.session)

    def test_payment_fact_survives_missing_enrollment_and_retries_metadata(self):
        context = self.context(do_enroll=False)
        self.assertEqual(self.purchase(context)["experiment_tracking_status"], "pending")
        self.assertEqual(len(self.session.exec(select(AppStoreRevenueEvent)).all()), 1)
        self.assertEqual(self.session.exec(select(ActivationJourneyAttribution)).all(), [])
        enrollment(ActivationJourneyEnrollmentRequest(journey=context), self.user, self.session)
        self.assertEqual(self.purchase(context), {"detail": "successfull"})
        self.assertEqual(len(self.session.exec(select(AppStoreRevenueEvent)).all()), 1)
        self.assertEqual(len(self.session.exec(select(ActivationJourneyAttribution)).all()), 1)
        self.assertEqual(self.session.exec(select(ExperimentConversion)).all(), [])

    def test_malformed_optional_metadata_never_rejects_verified_payment(self):
        context = self.context()
        cases = [{**context.model_dump(mode="json"), "variant": "invented"},
                 {**context.model_dump(mode="json"), "measurement_revision": 99},
                 {"unbounded": "x" * 10000}, ["not", "an", "object"], {},
                 {**context.model_dump(mode="json"), "eligible": False}]
        for index, invalid in enumerate(cases):
            with self.subTest(case=index):
                result = self.purchase(context, transaction=f"invalid-metadata-{index}", original=f"invalid-original-{index}", journey_override=invalid)
                self.assertEqual(result, {"detail": "successfull", "experiment_tracking_status": "conflict"})
                self.assertIsNotNone(self.session.get(Transaction, f"invalid-metadata-{index}"))
                self.assertIsNotNone(self.session.get(AppStoreRevenueEvent, f"invalid-metadata-{index}"))
        self.assertEqual(self.session.exec(select(ActivationJourneyAttribution)).all(), [])

    def test_http_invalid_metadata_is_success_not_fastapi_422(self):
        context = self.context()
        app = FastAPI()
        app.include_router(subscriptions_router, prefix="/subscriptions")
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_session] = lambda: self.session
        decoded = legacy.ExperimentReportingTests.decoded_transaction("http-verified")
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
            response = TestClient(app).post("/subscriptions/", json={"jws_payload": "synthetic-jws", "journey": {"variant": "wrong"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["experiment_tracking_status"], "conflict")
        self.assertIsNotNone(self.session.get(AppStoreRevenueEvent, "http-verified"))

    def test_metadata_database_failure_does_not_erase_payment(self):
        context = self.context()
        with patch("core.routers.subscriptions.attribute_journey_transaction", side_effect=RuntimeError("synthetic failure")):
            self.assertEqual(self.purchase(context)["experiment_tracking_status"], "pending")
        self.assertIsNotNone(self.session.get(AppStoreRevenueEvent, "transaction-journey"))

    def test_conflicting_journey_cannot_steal_original_subscription(self):
        first, second = self.context(), self.context()
        self.purchase(first, original="same-original")
        result = self.purchase(second, original="same-original", transaction="later-transaction")
        self.assertEqual(result["experiment_tracking_status"], "conflict")
        association = self.session.get(ActivationJourneyAttribution, "same-original")
        self.assertEqual(association.enrollment_id, first.exposure_id + ":v1")
        self.assertEqual(len(self.session.exec(select(AppStoreRevenueEvent)).all()), 2)

    def test_report_uses_all_mature_entrants_and_conditional_selection_separately(self):
        now = current_time_ms()
        selected = self.context(enrolled=now - 2 * DAY)
        self.context(enrolled=now - 2 * DAY)  # never searches: stays in denominator
        self.context(enrolled=now - 60_000)  # censored, never counted as failure
        selection = self.event(selected, "activation_journey_selected_flight", offset=5000, properties={"selection_eligible": True, "flight_identity": "a" * 64})
        self.send(self.event(selected), self.event(selected, "search_completed", properties={"has_results": True}), selection,
                  self.event(selected, "paywall_viewed", properties={"paywall_surface": "selected_flight"}))
        self.purchase(selected)
        self.checkout_proof(selected)
        self.send(self.event(selected, "flight_added", offset=12000, properties={"flight_identity": "a" * 64}),
                  self.event(selected, "screen_flight_detail_viewed", offset=14000, properties={"flight_identity": "a" * 64}))
        report = journey_summary(session=self.session, as_of_ms=current_time_ms())
        arm = report["arms"][0]
        self.assertEqual(arm["first_valid_selection_10m"]["numerator"], 1)
        self.assertEqual(arm["first_valid_selection_10m"]["denominator"], 2)
        self.assertEqual(arm["verified_trial_24h"]["denominator"], 2)
        self.assertEqual(arm["selected_flight_verified_trial_24h"]["denominator"], 1)
        self.assertEqual(arm["correct_post_purchase_activation_24h"]["numerator"], 1)
        self.assertEqual(arm["censored"]["ten_minutes"], 1)

    def test_wrong_flight_and_zero_results_cannot_be_reported_as_success(self):
        context = self.context(enrolled=current_time_ms() - 2 * DAY)
        self.send(self.event(context, "search_completed", properties={"has_results": False}),
                  self.event(context, "activation_journey_selected_flight", properties={"flight_identity": "a" * 64, "selection_eligible": True}))
        self.purchase(context)
        self.checkout_proof(context)
        self.send(self.event(context, "flight_added", offset=20000, properties={"flight_identity": "b" * 64}),
                  self.event(context, "screen_flight_detail_viewed", offset=21000, properties={"flight_identity": "b" * 64}))
        arm = journey_summary(session=self.session)["arms"][0]
        self.assertEqual(arm["successful_discovery_10m"]["numerator"], 0)
        self.assertEqual(arm["correct_post_purchase_activation_24h"]["numerator"], 0)
        self.assertEqual(arm["correct_post_purchase_activation_24h"]["missing_save"], 1)

    def test_d14_d30_native_product_receipts_partial_refunds_and_trial_to_paid(self):
        context = self.context(enrolled=current_time_ms() - 31 * DAY)
        self.context(enrolled=current_time_ms() - 31 * DAY)
        self.purchase(context, transaction="trial")
        self.purchase(context, trial=False, transaction="paid", purchase_at=context.enrolled_at_ms + 7 * DAY)
        fact = self.session.get(AppStoreRevenueEvent, "paid")
        fact.revoked_date_ms = context.enrolled_at_ms + 8 * DAY
        fact.revocation_percentage = 50_000
        self.session.add(fact)
        self.session.commit()
        arm = journey_summary(session=self.session)["arms"][0]
        for horizon in arm["mature_economics"]:
            economics = horizon["native_currency_actual_products"][0]
            self.assertEqual(horizon["mature_installations"], 2)
            self.assertEqual(economics["gross_milliunits"], 9990)
            self.assertEqual(economics["refund_milliunits"], 4995)
            self.assertEqual(economics["refund_adjusted_gross_per_installation_milliunits"], 2497.5)
            self.assertEqual(economics["trial_to_paid"]["rate"], 1.0)

    def test_retained_extraction_cap_fails_closed_not_partial_rates(self):
        self.context()
        self.context()
        with patch("core.services.activation_journey_reporting.ENTRY_CAP", 1):
            result = journey_summary(session=self.session)
        self.assertFalse(result["completeness"]["retained_extraction_complete"])
        self.assertEqual(result["arms"], [])

    def test_baseline_export_keyset_deduplicates_without_inventing_compatibility(self):
        old = legacy.ExperimentReportingTests.context()
        legacy.report_experiment_exposure(old, self.user, self.session)
        other = old.model_copy(update={"experiment_id": "paywall_flight_detail_2026_09", "variant": "control_current_paywall", "exposure_id": f"paywall_flight_detail_2026_09:{old.installation_id}"})
        legacy.report_experiment_exposure(other, self.user, self.session)
        cutoff = current_time_ms()
        first = baseline_manifest(session=self.session, analytics_environment="production", as_of_ms=cutoff, limit=1)
        second = baseline_manifest(session=self.session, analytics_environment="production", as_of_ms=cutoff, limit=1, after=first["next_cursor"])
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(first["unique_installations_at_cutoff"], 1)
        self.assertEqual(first["total_source_records_at_cutoff"], 2)
        self.assertFalse(first["baseline_ready"])
        self.assertNotEqual(first["records"][0]["record_id"], second["records"][0]["record_id"])
        self.assertEqual(first["records"][0]["installation_deduplication_key"], second["records"][0]["installation_deduplication_key"])

    def test_unknown_private_query_properties_remain_forbidden(self):
        context = self.context()
        with self.assertRaises(ValidationError):
            self.event(context, properties={"search_text": "private itinerary"})
        with self.assertRaises(ValidationError):
            self.event(context, properties={"_activation_journey": context.model_dump(mode="json")})

    def test_uppercase_swift_uuid_input_normalizes_but_exposure_is_canonical_lowercase(self):
        request = self.request()
        wire = request.model_dump(mode="json")
        wire["installation_id"] = wire["installation_id"].upper()
        wire["enrollment_event_id"] = wire["enrollment_event_id"].upper()
        context = assignment(ActivationJourneyAssignmentRequest.model_validate(wire), self.user, self.session)["journey"]
        self.assertEqual(str(context.installation_id), wire["installation_id"].lower())
        self.assertEqual(context.exposure_id, f"activation_journey_2026_09:{wire['installation_id'].lower()}")
        with self.assertRaises(ValidationError):
            ActivationJourneyContext.model_validate({**context.model_dump(mode="json"), "exposure_id": context.exposure_id.upper()})

    def test_pre_enrollment_fallback_cannot_be_taken_by_another_user_or_late_assignment(self):
        proposed = self.context(do_enroll=False)
        fallback = ActivationJourneyContext.model_validate({**proposed.model_dump(), "eligible": False, "randomized": False, "assignment_source": "configuration_fallback"})
        self.send(self.event(fallback))
        other = User(id="foreign-user")
        self.session.add(other)
        self.session.commit()
        with self.assertRaises(HTTPException) as failure:
            self.send(self.event(fallback), user=other)
        self.assertEqual(failure.exception.status_code, 403)
        original_request = ActivationJourneyAssignmentRequest.model_validate_json(self.session.exec(select(ActivationJourneyAssignment)).one().request_json)
        self.assertEqual(assignment(original_request, self.user, self.session)["journey"], fallback)

    def test_new_journey_cannot_enter_legacy_paywall_or_onboarding_ledgers(self):
        context = self.context()
        old = legacy.ExperimentReportingTests.context().model_copy(update={
            "installation_id": context.installation_id, "exposure_id": f"activation_experience_2026_08:{context.installation_id}"})
        with self.assertRaises(HTTPException) as failure:
            legacy.report_experiment_exposure(old, self.user, self.session)
        self.assertEqual(failure.exception.status_code, 409)
        paywall = old.model_copy(update={"experiment_id": "paywall_flight_detail_2026_09", "variant": "control_current_paywall",
                                        "exposure_id": f"paywall_flight_detail_2026_09:{context.installation_id}", "measurement_revision": 2})
        with self.assertRaises(HTTPException):
            report_experiment_enrollment(ExperimentEnrollmentRequest(experiment=paywall, measurement_revision=2, enrolled_at_ms=current_time_ms()), self.user, self.session)
        self.assertEqual(self.session.exec(select(ExperimentExposure)).all(), [])

    def test_legacy_preupgrade_goal_receipt_bytes_still_retry_idempotently(self):
        request = ExperimentGoalSelectionRequest(experiment=legacy.ExperimentReportingTests.context(), selected_goal_keys=["family_friends"],
                                                selected_at_ms=1000, confirmation_revision=1, confirmation_id=uuid4())
        report_experiment_goal_selection(request, self.user, self.session)
        old_wire = request.model_dump(mode="json")
        old_wire.pop("journey")
        original_digest = hashlib.sha256(json.dumps(old_wire, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        ledger = self.session.get(ExperimentGoalConfirmation, request.experiment.exposure_id)
        receipt = self.session.exec(select(ExperimentGoalConfirmationReceipt)).one()
        self.assertEqual(ledger.payload_sha256, original_digest)
        self.assertEqual(receipt.payload_sha256, original_digest)
        self.assertEqual(report_experiment_goal_selection(request, self.user, self.session)["status"], "idempotent")

    def test_new_reports_are_protected_and_only_local_qa_credentials_are_used(self):
        app = FastAPI()
        app.include_router(journey_router, prefix="/subscriptions")
        app.dependency_overrides[get_session] = lambda: self.session
        client = TestClient(app)
        for path in ("/subscriptions/activation-journey/summary", f"/subscriptions/activation-journey/baseline-manifest?as_of_ms={current_time_ms()}"):
            self.assertEqual(client.get(path).status_code, 401)
            self.assertEqual(client.get(path, headers={"Authorization": "Bearer test"}).status_code, 200)

    def test_trial_to_paid_is_censored_until_verified_trial_expiry(self):
        context = self.context(enrolled=current_time_ms() - 31 * DAY)
        self.purchase(context, transaction="long-trial")
        trial = self.session.get(AppStoreRevenueEvent, "long-trial")
        trial.expires_date_ms = context.enrolled_at_ms + 20 * DAY
        self.session.add(trial)
        self.session.commit()
        horizons = journey_summary(session=self.session)["arms"][0]["mature_economics"]
        self.assertEqual(horizons[0]["trial_maturity"]["verified_expired_trials"], 0)
        self.assertEqual(horizons[0]["trial_maturity"]["unmatured_at_horizon"], 1)
        self.assertEqual(horizons[1]["trial_maturity"]["verified_expired_trials"], 1)

    def test_purchase_after_a_to_b_backtracking_checks_frozen_checkout_b(self):
        context = self.context(enrolled=current_time_ms() - 2 * DAY)
        self.send(self.event(context, "activation_journey_selected_flight", properties={"selection_eligible": True, "flight_identity": "a" * 64}),
                  self.event(context, "flight_selected", offset=7000, properties={"flight_identity": "b" * 64}))
        self.purchase(context)
        self.checkout_proof(context, identity="b" * 64)
        self.send(self.event(context, "flight_added", offset=20000, properties={"flight_identity": "b" * 64}),
                  self.event(context, "screen_flight_detail_viewed", offset=21000, properties={"flight_identity": "b" * 64}))
        arm = journey_summary(session=self.session)["arms"][0]
        self.assertEqual(arm["correct_post_purchase_activation_24h"]["numerator"], 1)
        self.assertEqual(self.session.exec(select(ActivationJourneySelection)).one().flight_identity, "a" * 64)

    def test_backtracking_to_skip_purchase_does_not_reuse_first_selected_flight(self):
        context = self.context(enrolled=current_time_ms() - 2 * DAY)
        self.send(self.event(context, "activation_journey_selected_flight", properties={"selection_eligible": True, "flight_identity": "a" * 64}))
        self.purchase(context)
        self.checkout_proof(context, identity=None, surface="skip_flight")
        metric = journey_summary(session=self.session)["arms"][0]["correct_post_purchase_activation_24h"]
        self.assertEqual(metric["denominator"], 0)
        self.assertEqual(metric["not_selected_flight_purchase"], 1)
        self.assertEqual(metric["missing_save"], 0)

    def test_missing_checkout_link_is_unavailable_not_wrong_flight_failure(self):
        context = self.context(enrolled=current_time_ms() - 2 * DAY)
        self.purchase(context)
        metric = journey_summary(session=self.session)["arms"][0]["correct_post_purchase_activation_24h"]
        self.assertEqual(metric["denominator"], 0)
        self.assertEqual(metric["missing_checkout_attribution"], 1)
        self.assertIsNone(metric["rate"])

    def test_restore_link_does_not_own_original_account_checkout_attribution(self):
        now = current_time_ms()
        original_user = self.user
        restoring_user = User(id="restoring-user")
        self.session.add(restoring_user)
        self.session.commit()
        self.user = restoring_user
        context = self.context(enrolled=now - 2 * DAY)
        self.user = original_user
        decoded = legacy.ExperimentReportingTests.decoded_transaction("restore-missing-metadata")
        decoded.appAccountToken = original_user.id
        decoded.purchaseDate = now - DAY
        decoded.originalPurchaseDate = decoded.purchaseDate
        decoded.signedDate = decoded.purchaseDate + 1000
        decoded.expiresDate = now + DAY
        decoded.price = 0
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
            self.assertEqual(create_or_update_transaction(CreateTransactionRequest(jws_payload="synthetic"), original_user, self.session), {"detail": "successfull"})
            result = create_or_update_transaction(CreateTransactionRequest(jws_payload="synthetic", journey=context), restoring_user, self.session)
        self.assertEqual(result, {"detail": "successfull", "experiment_tracking_status": "conflict"})
        self.assertEqual(len(self.session.exec(select(AppStoreRevenueEvent)).all()), 1)
        self.assertEqual(self.session.exec(select(ActivationJourneyAttribution)).all(), [])
        self.assertIsNotNone(restoring_user.premium_valid_until)

    def test_operational_override_never_changes_original_assignment(self):
        request = self.request()
        with patch.object(settings, "ACTIVATION_JOURNEY_PRODUCTION_ENROLLMENT_ENABLED", True), patch.object(settings, "ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT", 0):
            original = assignment(request, self.user, self.session)
        with patch.object(settings, "ACTIVATION_JOURNEY_FORCE_STANDARD_PAYWALL", True), patch.object(settings, "ACTIVATION_JOURNEY_OPERATIONAL_CONFIG_VERSION", "incident_2"):
            overridden = assignment(request, self.user, self.session)
        self.assertEqual(original["journey"], overridden["journey"])
        self.assertFalse(original["force_standard_paywall"])
        self.assertTrue(overridden["force_standard_paywall"])
        self.assertEqual(overridden["operational_config_version"], "incident_2")

    def test_later_goal_confirmation_cannot_replace_answer_in_old_cutoff(self):
        context = self.context("goals_flight_detail")
        cutoff = current_time_ms()
        data = ExperimentGoalSelectionRequest(journey=context, selected_goal_keys=["family_friends"], selected_at_ms=cutoff,
                                             confirmation_revision=1, confirmation_id=uuid4())
        with patch("core.services.activation_journey.current_time_ms", return_value=cutoff + 1000):
            report_experiment_goal_selection(data, self.user, self.session)
        goals = journey_summary(session=self.session, as_of_ms=cutoff)["arms"][1]["goals"]
        self.assertEqual(goals["confirmed_installations"], 0)
        self.assertEqual(goals["final_revision_after_cutoff_unknown"], 1)
        self.assertEqual(goals["final_choice_installations"], {})

    def test_aged_cohort_event_rates_are_unavailable_not_purged_zeroes(self):
        context = self.context(enrolled=current_time_ms() - 2 * DAY)
        self.purchase(context)
        with patch("core.services.activation_journey_reporting.current_time_ms", return_value=current_time_ms() + 91 * DAY):
            arm = journey_summary(session=self.session)["arms"][0]
        self.assertIsNone(arm["search_reach_10m"]["rate"])
        self.assertEqual(arm["paywall_reach_24h"]["status"], "unavailable_diagnostic_retention_gap")
        self.assertIsNone(arm["correct_post_purchase_activation_24h"]["denominator"])
        self.assertEqual(arm["verified_trial_24h"]["numerator"], 1)


class JourneyConcurrentTests(unittest.TestCase):
    def test_independent_workers_cannot_reassign_one_installation(self):
        with tempfile.TemporaryDirectory(prefix="sofly-journey-concurrency-") as directory:
            engine = create_engine(f"sqlite:///{Path(directory) / 'synthetic.db'}", connect_args={"check_same_thread": False, "timeout": 5})
            SQLModel.metadata.create_all(engine)
            with Session(engine) as session:
                session.add(User(id="worker-owner"))
                session.commit()
            request = ActivationJourneyAssignmentRequest(installation_id=uuid4(), enrollment_event_id=uuid4(),
                enrolled_at_ms=current_time_ms(), app_version="3.9", build_number="120", analytics_environment="production", is_new_installation=True)
            barrier = Barrier(2)
            def write():
                with Session(engine) as session:
                    user = session.get(User, "worker-owner")
                    barrier.wait()
                    return assignment(request, user, session)["journey"]
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(write) for _ in range(2)]
                results = [future.result(timeout=10) for future in futures]
            self.assertEqual(results[0], results[1])
            with Session(engine) as session:
                self.assertEqual(len(session.exec(select(ActivationJourneyAssignment)).all()), 1)
                self.assertEqual(len(session.exec(select(ActivationJourneyIdentity)).all()), 1)
            engine.dispose()
