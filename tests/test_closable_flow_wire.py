"""Schema-28 closable-flow wire payloads through the backend contract, end to end.

The synthetic catalog mirrors the iOS encoder's output (journey object plus the
allowlisted properties) for every event in the spec's event catalog, in both
arms, including a switch from annual to weekly. Set CLOSABLE_FLOW_WIRE_FIXTURES
to a directory of JSON exported by the app's AnalyticsWireCoverageChecks to
replay the real Swift-encoded payloads through the same validation and ingestion.
"""

import json
import os
import unittest
from pathlib import Path
from uuid import UUID, uuid4
from unittest.mock import patch

from tests import test_experiment_reporting as legacy
from tests import test_closable_flow_assignment as assignment_tests

from pydantic import ValidationError
from sqlmodel import select

from core import closable_flow_policy as policy
from core.activation_journey_contract import ActivationJourneyAssignmentRequest, ActivationJourneyContext, canonical_context
from core.models.activation_journey import ActivationJourneyAssignment, ActivationJourneyEnrollment
from core.models.experiment import ExperimentDiagnosticEvent
from core.routers.experiment_diagnostics import DiagnosticBatch, DiagnosticEvent, record_diagnostic_events

ANNUAL, WEEKLY, LIFETIME = policy.ANNUAL_PRODUCT_ID, policy.WEEKLY_PRODUCT_ID, policy.LIFETIME_PRODUCT_ID
IDENTITY = "c" * 64


class WireCatalog:
    """Builds payload dictionaries exactly as JSON on the wire."""

    def __init__(self, context):
        self.context = context
        self.journey = json.loads(context.model_dump_json())
        self.clock = context.enrolled_at_ms
        self.events = []

    def add(self, name, *, presentation=None, attempt=None, identity=None, at=None, **properties):
        self.clock = at if at is not None else self.clock + 1_000
        payload = {
            "event_id": str(identity or uuid4()), "event_name": name, "occurred_at_ms": self.clock,
            "installation_id": str(self.context.installation_id), "app_version": self.context.app_version,
            "build_number": self.context.build_number, "analytics_environment": self.context.analytics_environment,
            "build_configuration": "release", "journey": self.journey,
            # The app merges the randomized arm into every schema-28 event.
            "properties": {"event_schema_version": 28,
                           **({"arm": self.context.variant} if self.context.randomized else {}), **properties},
        }
        if presentation:
            payload["paywall_presentation_id"] = str(presentation)
        if attempt:
            payload["checkout_attempt_id"] = str(attempt)
        self.events.append(payload)
        return payload

    def exposure(self):
        onboarding, paywall, _ = policy.SCOPES[self.context.variant]
        return self.add("activation_experiment_exposed", identity=self.context.enrollment_event_id,
                        at=self.context.enrolled_at_ms, exposure_scope=policy.EXPOSURE_SCOPE,
                        measurement_revision=self.context.measurement_revision,
                        assignment_source=self.context.assignment_source, effective_onboarding=onboarding,
                        effective_paywall=paywall, effective_offer=policy.offer_scope(self.context.measurement_revision),
                        operational_override="none", config_version=self.context.config_version)

    def paywall(self, presentation, source, *, surface="selected_flight", layout=None):
        onboarding, paywall, _ = policy.SCOPES[self.context.variant]
        self.add("paywall_viewed", presentation=presentation, source=source,
                 layout=layout or ("closable_one_screen" if paywall == "closable_one_screen" else "cockpit_two_step"),
                 effective_onboarding=onboarding, effective_paywall=paywall,
                 effective_offer=policy.offer_scope(self.context.measurement_revision), operational_override="none",
                 paywall_surface=surface, offer_eligibility="eligible", assigned_product_id=ANNUAL,
                 displayed_product_id=ANNUAL, config_version=self.context.config_version,
                 measurement_revision=self.context.measurement_revision,
                 **({"flight_identity": IDENTITY} if surface == "selected_flight" else {}))
        lifetime = policy.offers_lifetime(self.context.measurement_revision)
        self.add("paywall_products_loaded", presentation=presentation, status="loaded", lifetime_offered=lifetime,
                 available_plan_count=3 if lifetime else 2, visible_plan_count=3 if lifetime else 2)
        self.add("subscription_product_selected", presentation=presentation, selection_method="default",
                 product_id=ANNUAL, displayed_product_id=ANNUAL, assigned_product_id=ANNUAL)

    def checkout(self, presentation, product, outcome, *, transaction=None, trial=False):
        attempt = uuid4()
        self.add("af_initiated_checkout", presentation=presentation, attempt=attempt, product_id=product,
                 displayed_product_id=product, assigned_product_id=ANNUAL, source="paywall")
        self.add("checkout_attempt_completed", presentation=presentation, attempt=attempt, outcome=outcome,
                 product_id=product, displayed_product_id=product, assigned_product_id=ANNUAL)
        if transaction:
            self.add("af_start_trial" if trial else "af_purchase", presentation=presentation, attempt=attempt,
                     product_id=product, transaction_id=transaction, original_transaction_id=transaction,
                     purchase_environment="Production", starts_trial=trial, trial_duration_days=7 if trial else 0)


class ClosableFlowWireTests(unittest.TestCase):
    setUp = legacy.ExperimentReportingTests.setUp
    tearDown = legacy.ExperimentReportingTests.tearDown
    switches = assignment_tests.ClosableFlowAssignmentTests.switches
    request = assignment_tests.ClosableFlowAssignmentTests.request
    assign = assignment_tests.ClosableFlowAssignmentTests.assign

    def context(self, arm, **changes):
        request = self.request(installation=assignment_tests.installation_in(arm))
        return self.assign(request, CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, **changes)["journey"]

    def deliver(self, events):
        accepted = 0
        for start in range(0, len(events), 25):
            batch = DiagnosticBatch.model_validate({"events": events[start:start + 25]})
            accepted += record_diagnostic_events(batch, self.user, self.session)["accepted"]
        return accepted

    def test_closable_arm_catalog_including_annual_to_weekly_switch(self):
        wire = WireCatalog(self.context(policy.TEST))
        wire.exposure()
        wire.add("onboarding_started", source="onboarding")
        wire.add("onboarding_step_viewed", step_key="welcome_treatment", step=1, total_steps=3)
        journey = str(uuid4())
        wire.add("onboarding_step_viewed", step_key="activation_search", step=2, total_steps=3)
        wire.add("af_search", query_type="flight_number", search_journey_id=journey, search_attempt_number=1, source="onboarding")
        wire.add("search_failed", failure_reason="provider_unavailable", provider_outcome="unavailable",
                 search_journey_id=journey, search_attempt_number=1)
        wire.add("activation_experiment_action", action="airport_list_shown", step_key="activation_search", source="provider_failure")
        wire.add("activation_experiment_action", action="airport_board_opened", step_key="activation_search")
        wire.add("no_search_results", failure_reason="results_filtered_out", search_journey_id=journey, search_attempt_number=2)
        wire.add("search_completed", has_results=True, query_type="flight_number", search_journey_id=journey,
                 search_attempt_number=3, provider_result_count=2, filtered_result_count=0, normalization_applied=False)
        for action in ("demo_flight_check_shown", "demo_flight_confirmed"):
            wire.add("activation_experiment_action", action=action, step_key="activation_search")
        wire.add("flight_selected", flight_identity=IDENTITY, search_journey_id=journey, search_attempt_number=3, source="onboarding")
        wire.add("activation_journey_selected_flight", selection_eligible=True, flight_identity=IDENTITY,
                 search_journey_id=journey, search_attempt_number=3, effective_onboarding="own_flight_preview",
                 effective_paywall="closable_one_screen", effective_offer="annual_weekly")
        wire.add("onboarding_step_viewed", step_key="activation_preview", step=3, total_steps=3)
        wire.add("activation_experiment_action", action="preview_not_my_flight", step_key="activation_preview")
        first = uuid4()
        wire.paywall(first, "universal_onboarding")
        wire.checkout(first, ANNUAL, "cancelled")
        panel = str(uuid4())
        for action in ("retry_panel_shown", "retry_checkout_tapped"):
            wire.add("experience_action", presentation=first, screen="paywall", action=action,
                     experience_presentation_id=panel, product_id=ANNUAL, source="universal_onboarding")
        wire.checkout(first, ANNUAL, "verified", transaction="wire-trial-1", trial=True)
        wire.add("notification_permission_result", status="authorized", source="trial_activation")
        wire.add("paywall_dismissed", presentation=first, source="purchase_success")
        wire.add("flight_added", flight_identity=IDENTITY, flight_id=42, search_journey_id=journey, source="universal_onboarding")
        wire.add("screen_flight_detail_viewed", flight_identity=IDENTITY, flight_id=42, search_journey_id=journey)
        second = uuid4()
        wire.paywall(second, "paywall_gated_number_search")
        wire.add("subscription_product_selected", presentation=second, selection_method="user", product_id=WEEKLY,
                 displayed_product_id=WEEKLY, assigned_product_id=ANNUAL)
        wire.add("paywall_dismissed", presentation=second, source="close")
        for action in ("reminder_offer_shown", "reminder_opted_in", "track_tapped", "alerts_tapped"):
            wire.add("experience_action", screen="pending_flight", action=action, flight_identity=IDENTITY,
                     experience_presentation_id=str(uuid4()), source="pending_flight")
        wire.add("notification_permission_result", status="authorized", source="departure_reminder")
        wire.add("flight_notification_scheduling", notification_type="departure_paywall_reminder", outcome="scheduled", stage="local")
        wire.add("push_opened", source="local", notification_type="departure_paywall_reminder",
                 notification_id="departure-offer", action="default_tap", notification_open_delay="1h_to_1d")
        wire.add("flight_add_blocked", reason="paywall", source="pending_flight", flight_identity=IDENTITY)
        third = uuid4()
        wire.paywall(third, "paywall_gated_departure_reminder")
        wire.add("subscription_product_selected", presentation=third, selection_method="user", product_id=WEEKLY,
                 displayed_product_id=WEEKLY, assigned_product_id=ANNUAL)
        wire.checkout(third, WEEKLY, "verified", transaction="wire-weekly-1")
        self.assertEqual(self.deliver(wire.events), len(wire.events))
        self.assertEqual(self.deliver(wire.events), 0)
        rows = self.session.exec(select(ExperimentDiagnosticEvent)).all()
        self.assertEqual(len(rows), len(wire.events))
        self.assertTrue(all(json.loads(row.properties_json)["_activation_journey"]["experiment_id"] == policy.EXPERIMENT_ID
                            for row in rows))
        entry = self.session.exec(select(ActivationJourneyEnrollment)).one()
        self.assertEqual((entry.variant, entry.randomized, entry.measurement_revision), (policy.TEST, True, 1))

    def test_control_arm_catalog_and_lifetime_revision(self):
        wire = WireCatalog(self.context(policy.CONTROL))
        wire.exposure()
        wire.add("onboarding_started", source="onboarding")
        wire.add("review_prompt_requested", source="onboarding_sharing")
        skip = uuid4()
        wire.paywall(skip, "universal_onboarding", surface="skip_flight")
        wire.checkout(skip, ANNUAL, "cancelled")
        wire.checkout(skip, ANNUAL, "pending")
        lifetime = WireCatalog(self.context(policy.CONTROL, CLOSABLE_FLOW_MEASUREMENT_REVISION=2,
                                            CLOSABLE_FLOW_LIFETIME_ENABLED=True))
        lifetime.exposure()
        presentation = uuid4()
        lifetime.paywall(presentation, "universal_onboarding")
        lifetime.add("subscription_product_selected", presentation=presentation, selection_method="user",
                     product_id=LIFETIME, displayed_product_id=LIFETIME, assigned_product_id=ANNUAL)
        lifetime.checkout(presentation, LIFETIME, "verified", transaction="wire-lifetime-1")
        events = wire.events + lifetime.events
        self.assertEqual(self.deliver(events), len(events))
        entries = self.session.exec(select(ActivationJourneyEnrollment)).all()
        self.assertEqual(sorted((row.measurement_revision, row.config_version) for row in entries),
                         [(1, "closable_flow_v1"), (2, "closable_flow_v2")])

    def test_wire_rejects_old_schema_raw_text_and_noncanonical_exposure(self):
        wire = WireCatalog(self.context(policy.TEST))
        exposure = wire.exposure()
        search = wire.add("af_search", query_type="flight_number")
        for payload in ({**search, "properties": {**search["properties"], "event_schema_version": 27}},
                        {**search, "properties": {**search["properties"], "query": "BA178"}},
                        {**search, "properties": {**search["properties"], "search_text": "BA178 LHR"}},
                        {**exposure, "event_id": str(uuid4())},
                        {**exposure, "event_name": "activation_journey_enrolled"}):
            with self.subTest(payload=payload["properties"]), self.assertRaises(ValidationError):
                DiagnosticEvent.model_validate(payload)

    def test_exported_ios_fixtures_validate_when_provided(self):
        directory = os.environ.get("CLOSABLE_FLOW_WIRE_FIXTURES")
        if not directory:
            self.skipTest("Set CLOSABLE_FLOW_WIRE_FIXTURES to the iOS AnalyticsWireCoverageChecks export")
        payloads = []
        for path in sorted(Path(directory).glob("*.json")):
            document = json.loads(path.read_text())
            items = document.get("events", [document]) if isinstance(document, dict) else document
            for item in items:
                if item.get("properties", {}).get("event_schema_version", 0) < 28:
                    continue
                # The Swift checks run as a Debug harness that labels its rows
                # testflight; shipped TestFlight and App Store builds send release.
                if item.get("analytics_environment") != "development":
                    item = {**item, "build_configuration": "release"}
                payloads.append((path.name, item))
        self.assertTrue(payloads, "no schema-28 payloads exported")
        for name, payload in payloads:
            with self.subTest(fixture=name, event=payload.get("event_name")):
                DiagnosticEvent.model_validate(payload)
        # End to end: seed each server answer as the assignment endpoint would
        # have stored it, then ingest every installation's events in order.
        installations = {}
        for _, payload in payloads:
            installations.setdefault(payload["installation_id"], []).append(payload)
        latest = max(payload["occurred_at_ms"] for _, payload in payloads)
        for events in installations.values():
            journey = events[0].get("journey")
            if journey and journey["assignment_source"] in ("server_assignment", "server_disabled"):
                context = ActivationJourneyContext.model_validate(journey)
                request = ActivationJourneyAssignmentRequest(
                    installation_id=context.installation_id, enrollment_event_id=context.enrollment_event_id,
                    enrolled_at_ms=context.enrolled_at_ms, app_version=context.app_version,
                    build_number=context.build_number, analytics_environment=context.analytics_environment,
                    is_new_installation=True)
                self.session.add(ActivationJourneyAssignment(
                    id=context.exposure_id + ":v1", installation_id=str(context.installation_id), user_id=self.user.id,
                    context_json=canonical_context(context), request_json=request.model_dump_json(),
                    created_at_ms=context.enrolled_at_ms))
                self.session.commit()
            ordered = sorted(events, key=lambda event: (event["occurred_at_ms"],
                                                        event["event_name"] != "activation_experiment_exposed"))
            with patch("core.routers.experiment_diagnostics.current_time_ms", return_value=latest + 1_000), \
                 patch("core.services.activation_journey.current_time_ms", return_value=latest + 1_000):
                self.assertEqual(self.deliver(ordered), len(ordered))
        exposed = {str(UUID(payload["installation_id"])) for _, payload in payloads
                   if payload["event_name"] == "activation_experiment_exposed"}
        entries = self.session.exec(select(ActivationJourneyEnrollment)).all()
        self.assertEqual({entry.installation_id for entry in entries if entry.randomized}, exposed)


if __name__ == "__main__":
    unittest.main()
