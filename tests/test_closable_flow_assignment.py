"""closable_flow_2026_10 assignment, switches and diagnostics; in-memory only."""

import hashlib
import json
import unittest
from contextlib import ExitStack
from uuid import UUID, uuid4
from unittest.mock import patch

from tests import test_experiment_reporting as legacy

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import select

from core import closable_flow_policy as policy
from core.activation_journey_contract import (
    ActivationJourneyAssignmentRequest, ActivationJourneyContext, ActivationJourneyEnrollmentRequest, canonical_context,
)
from core.config import Settings, settings
from core.dependency import get_current_user
from core.models import get_session
from core.models.activation_journey import (
    ActivationJourneyAssignment, ActivationJourneyAttribution, ActivationJourneyEnrollment, ActivationJourneyIdentity,
)
from core.models.apple_ads import AppStoreRevenueEvent
from core.models.experiment import ExperimentConversion, ExperimentDiagnosticEvent, ExperimentExposure, current_time_ms
from core.models.user import User
from core.routers.activation_journey import assignment, closable_flow_assignment, enrollment, router as journey_router
from core.routers.experiment_diagnostics import DiagnosticBatch, DiagnosticEvent, DiagnosticProperties, record_diagnostic_events
from core.routers.subscriptions import CreateTransactionRequest, create_or_update_transaction
from core.services.closable_flow_reporting import closable_flow_summary

DAY = 86_400_000
FLAGS = {
    "CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED": False, "CLOSABLE_FLOW_NONPRODUCTION_ENROLLMENT_ENABLED": False,
    "CLOSABLE_FLOW_TEST_PERCENT": 50, "CLOSABLE_FLOW_NONPRODUCTION_TEST_PERCENT": 50,
    "CLOSABLE_FLOW_MEASUREMENT_REVISION": 1, "CLOSABLE_FLOW_FORCE_CONTROL_DELIVERY": False,
    "CLOSABLE_FLOW_LIFETIME_ENABLED": False, "CLOSABLE_FLOW_OPERATIONAL_CONFIG_VERSION": "1",
}


def installation_in(arm, percent=50):
    """A fresh installation ID whose bucket lands in the given arm."""
    while True:
        installation = uuid4()
        if policy.assigned_variant(installation, percent) == arm:
            return installation


class ClosableFlowAssignmentTests(unittest.TestCase):
    setUp = legacy.ExperimentReportingTests.setUp
    tearDown = legacy.ExperimentReportingTests.tearDown

    def switches(self, **changes):
        stack = ExitStack()
        for key, value in {**FLAGS, **changes}.items():
            stack.enter_context(patch.object(settings, key, value))
        return stack

    def request(self, *, environment="production", installation=None, enrolled=None):
        return ActivationJourneyAssignmentRequest(
            installation_id=installation or uuid4(), enrollment_event_id=uuid4(),
            enrolled_at_ms=enrolled if enrolled is not None else current_time_ms() - 3_600_000,
            app_version="3.10.0", build_number="146", analytics_environment=environment, is_new_installation=True)

    def assign(self, request, user=None, **changes):
        with self.switches(**changes), patch("core.services.activation_journey.current_time_ms",
                                             return_value=request.enrolled_at_ms + 1000):
            return closable_flow_assignment(request, user or self.user, self.session)

    def enrolled(self, arm=policy.TEST, *, environment="production", enrolled=None, **changes):
        request = self.request(environment=environment, installation=installation_in(arm), enrolled=enrolled)
        flag = "CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED" if environment == "production" else "CLOSABLE_FLOW_NONPRODUCTION_ENROLLMENT_ENABLED"
        context = self.assign(request, **{flag: True, **changes})["journey"]
        self.send(self.exposure(context))
        return context

    def event(self, context, name="af_search", *, offset=1000, identity=None, properties=None, schema=28,
              attempt=None, presentation=None):
        return DiagnosticEvent(
            event_id=identity or uuid4(), event_name=name, occurred_at_ms=context.enrolled_at_ms + offset,
            installation_id=context.installation_id, app_version=context.app_version, build_number=context.build_number,
            analytics_environment=context.analytics_environment,
            build_configuration="debug" if context.analytics_environment == "development" else "release",
            journey=context, properties={"event_schema_version": schema, **(properties or {})},
            checkout_attempt_id=attempt,
            paywall_presentation_id=presentation or (uuid4() if name.startswith("paywall_") or name == "subscription_product_selected" else None))

    def exposure(self, context, **changes):
        scope = policy.SCOPES[context.variant]
        properties = {"exposure_scope": policy.EXPOSURE_SCOPE, "measurement_revision": context.measurement_revision,
                      "assignment_source": context.assignment_source, "effective_onboarding": scope[0],
                      "effective_paywall": scope[1], "effective_offer": policy.offer_scope(context.measurement_revision),
                      "operational_override": "none", **changes}
        return self.event(context, "activation_experiment_exposed", offset=0, identity=context.enrollment_event_id,
                          properties=properties)

    def send(self, *events, user=None):
        return record_diagnostic_events(DiagnosticBatch(events=list(events)), user or self.user, self.session)

    def test_defaults_are_off_and_registry_matches_the_owner_decisions(self):
        defaults = Settings()
        for key, value in FLAGS.items():
            self.assertEqual(getattr(defaults, key), value, key)
        self.assertEqual(policy.REVISIONS, {
            1: ("closable_flow_v1", "annual_weekly"), 2: ("closable_flow_v2", "annual_weekly_lifetime"),
            3: ("closable_flow_v3", "annual_weekly"), 4: ("closable_flow_v4", "annual_weekly_lifetime"),
            5: ("matched_paywall_v1", "annual_weekly"), 6: ("matched_paywall_v2", "annual_weekly_lifetime")})
        self.assertEqual(policy.SCOPES, {
            "control_hard_paywall": ("search_first", "hard", "not_asked"),
            "closable_own_flight": ("own_flight_preview", "closable_one_screen", "not_asked"),
            "matched_hard_paywall": ("own_flight_preview", "hard_one_screen", "not_asked"),
            "matched_soft_paywall": ("own_flight_preview", "soft_one_screen", "not_asked")})

    def test_startup_rejects_unknown_revision_lifetime_without_switch_and_bad_percent(self):
        on = {"CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED": True}
        for changes in ({"CLOSABLE_FLOW_MEASUREMENT_REVISION": 7}, {"CLOSABLE_FLOW_MEASUREMENT_REVISION": 0},
                        {"CLOSABLE_FLOW_MEASUREMENT_REVISION": 2, **on}, {"CLOSABLE_FLOW_MEASUREMENT_REVISION": 4, **on},
                        {"CLOSABLE_FLOW_MEASUREMENT_REVISION": 4, "CLOSABLE_FLOW_NONPRODUCTION_ENROLLMENT_ENABLED": True},
                        {"CLOSABLE_FLOW_TEST_PERCENT": 101}, {"CLOSABLE_FLOW_NONPRODUCTION_TEST_PERCENT": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                Settings(**changes)
        for revision, lifetime in ((1, False), (1, True), (2, True), (3, False), (4, True)):
            configured = Settings(CLOSABLE_FLOW_MEASUREMENT_REVISION=revision, CLOSABLE_FLOW_LIFETIME_ENABLED=lifetime, **on)
            self.assertEqual(configured.CLOSABLE_FLOW_MEASUREMENT_REVISION, revision)
        # Emergency Lifetime kill: allowed under a Lifetime revision only while
        # no new install can be randomized into it.
        for changes in ({}, {**on, "CLOSABLE_FLOW_FORCE_CONTROL_DELIVERY": True}):
            configured = Settings(CLOSABLE_FLOW_MEASUREMENT_REVISION=4, CLOSABLE_FLOW_LIFETIME_ENABLED=False, **changes)
            self.assertFalse(configured.CLOSABLE_FLOW_LIFETIME_ENABLED)
        # A runtime patch that bypasses startup validation still fails closed.
        with self.assertRaises(HTTPException) as failure:
            self.assign(self.request(), CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, CLOSABLE_FLOW_MEASUREMENT_REVISION=2)
        self.assertEqual(failure.exception.status_code, 503)
        self.assertEqual(self.session.exec(select(ActivationJourneyAssignment)).all(), [])

    def test_disabled_enrollment_gives_control_not_randomized_and_stays_sticky(self):
        request = self.request()
        first = self.assign(request)
        context = first["journey"]
        self.assertEqual((context.variant, context.assignment_source, context.eligible, context.randomized),
                         ("control_hard_paywall", "server_disabled", False, False))
        self.assertEqual((context.experiment_id, context.measurement_revision, context.config_version),
                         ("closable_flow_2026_10", 1, "closable_flow_v1"))
        self.assertEqual(first, {"journey": context, "enrollment_enabled": False, "force_control_delivery": False,
                                 "lifetime_delivery_enabled": False, "operational_config_version": "1"})
        replay = self.assign(request, CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, CLOSABLE_FLOW_TEST_PERCENT=100)
        self.assertEqual(replay["journey"], context)
        self.assertEqual(self.session.exec(select(ActivationJourneyEnrollment)).all(), [])

    def test_bucketing_is_the_specified_hash_and_first_proposal_wins(self):
        for arm in policy.VARIANTS:
            installation = installation_in(arm)
            digest = hashlib.sha256(f"closable_flow_2026_10:{installation}".encode()).digest()
            self.assertEqual(int.from_bytes(digest[:8], "big") % 100 < 50, arm == policy.TEST)
            request = self.request(installation=installation)
            context = self.assign(request, CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True)["journey"]
            self.assertEqual((context.variant, context.assignment_source), (arm, "server_assignment"))
            self.assertEqual((context.intended_onboarding, context.intended_paywall, context.goals_status), policy.SCOPES[arm])
            stored = self.session.get(ActivationJourneyAssignment, context.exposure_id + ":v1").context_json
            for changes in ({"CLOSABLE_FLOW_TEST_PERCENT": 0}, {"CLOSABLE_FLOW_TEST_PERCENT": 100},
                            {"CLOSABLE_FLOW_MEASUREMENT_REVISION": 3}, {"CLOSABLE_FLOW_FORCE_CONTROL_DELIVERY": True}):
                self.assertEqual(self.assign(request, CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, **changes)["journey"], context)
            self.assertEqual(self.session.get(ActivationJourneyAssignment, context.exposure_id + ":v1").context_json, stored)
        variants = [policy.assigned_variant(UUID(int=number), 50) for number in range(1, 2001)]
        self.assertTrue(0.45 < variants.count(policy.TEST) / len(variants) < 0.55)

    def test_replay_with_changed_capture_or_another_account_is_rejected(self):
        request = self.request()
        self.assign(request, CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True)
        with self.assertRaises(HTTPException) as changed:
            self.assign(request.model_copy(update={"build_number": "147"}), CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True)
        self.assertEqual(changed.exception.status_code, 409)
        other = User(id="another-user")
        self.session.add(other)
        self.session.commit()
        with self.assertRaises(HTTPException) as foreign:
            self.assign(request, user=other, CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True)
        self.assertEqual(foreign.exception.status_code, 403)

    def test_kill_switch_disables_new_enrollment_and_reports_override_without_reassigning(self):
        context = self.enrolled(policy.TEST)
        response = self.assign(self.request(installation=context.installation_id).model_copy(update={
            "enrollment_event_id": context.enrollment_event_id, "enrolled_at_ms": context.enrolled_at_ms}),
            CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, CLOSABLE_FLOW_FORCE_CONTROL_DELIVERY=True,
            CLOSABLE_FLOW_OPERATIONAL_CONFIG_VERSION="2")
        self.assertEqual(response["journey"], context)
        self.assertEqual((response["force_control_delivery"], response["operational_config_version"]), (True, "2"))
        fresh = self.assign(self.request(installation=installation_in(policy.TEST)),
                            CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, CLOSABLE_FLOW_FORCE_CONTROL_DELIVERY=True)
        self.assertEqual((fresh["journey"].variant, fresh["journey"].assignment_source), ("control_hard_paywall", "server_disabled"))
        # Forced-control presentations keep the assigned arm in analysis.
        self.send(self.event(context, "paywall_viewed", properties={
            "operational_override": "forced_control", "effective_paywall": "hard", "effective_offer": "annual_weekly",
            "paywall_surface": "skip_flight"}))
        self.assertEqual(self.session.get(ActivationJourneyEnrollment, context.exposure_id + ":v1").variant, policy.TEST)

    def test_nonproduction_percent_and_switch_never_affect_production(self):
        changes = {"CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED": True, "CLOSABLE_FLOW_NONPRODUCTION_ENROLLMENT_ENABLED": True,
                   "CLOSABLE_FLOW_TEST_PERCENT": 0, "CLOSABLE_FLOW_NONPRODUCTION_TEST_PERCENT": 100}
        production = self.assign(self.request(), **changes)["journey"]
        testflight = self.assign(self.request(environment="testflight"), **changes)["journey"]
        self.assertEqual((production.variant, production.randomized), (policy.CONTROL, True))
        self.assertEqual((testflight.variant, testflight.randomized), (policy.TEST, True))
        only_nonproduction = self.assign(self.request(), CLOSABLE_FLOW_NONPRODUCTION_ENROLLMENT_ENABLED=True,
                                         CLOSABLE_FLOW_NONPRODUCTION_TEST_PERCENT=100)["journey"]
        self.assertEqual(only_nonproduction.assignment_source, "server_disabled")

    def test_revision_comes_from_the_setting_and_lifetime_switch_only_affects_its_scope(self):
        lifetime = {"CLOSABLE_FLOW_MEASUREMENT_REVISION": 2, "CLOSABLE_FLOW_LIFETIME_ENABLED": True}
        rev1 = self.enrolled(policy.CONTROL)
        rev2_request = self.request(installation=installation_in(policy.TEST))
        response = self.assign(rev2_request, CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, **lifetime)
        rev2 = response["journey"]
        self.assertEqual((rev2.measurement_revision, rev2.config_version, response["lifetime_delivery_enabled"]),
                         (2, "closable_flow_v2", True))
        # Lifetime off again means a new revision for new installs; rev-2 installs keep
        # their revision and only receive lifetime_delivery_enabled=false.
        off = {"CLOSABLE_FLOW_MEASUREMENT_REVISION": 3, "CLOSABLE_FLOW_LIFETIME_ENABLED": False}
        refreshed = self.assign(rev2_request, CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, **off)
        self.assertEqual((refreshed["journey"], refreshed["lifetime_delivery_enabled"]), (rev2, False))
        rev1_refresh = self.assign(self.request(installation=rev1.installation_id).model_copy(update={
            "enrollment_event_id": rev1.enrollment_event_id, "enrolled_at_ms": rev1.enrolled_at_ms}),
            CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, **lifetime)
        self.assertFalse(rev1_refresh["lifetime_delivery_enabled"])
        rev3 = self.assign(self.request(), CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, **off)["journey"]
        self.assertEqual((rev3.measurement_revision, rev3.config_version), (3, "closable_flow_v3"))
        self.assertEqual(self.session.get(ActivationJourneyEnrollment, rev1.exposure_id + ":v1").measurement_revision, 1)

    def test_emergency_lifetime_kill_under_the_last_lifetime_revision_stops_enrollment(self):
        lifetime = {"CLOSABLE_FLOW_MEASUREMENT_REVISION": 4, "CLOSABLE_FLOW_LIFETIME_ENABLED": True}
        context = self.enrolled(policy.TEST, **lifetime)
        replay = self.request(installation=context.installation_id).model_copy(update={
            "enrollment_event_id": context.enrollment_event_id, "enrolled_at_ms": context.enrolled_at_ms})
        self.assertTrue(self.assign(replay, CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, **lifetime)["lifetime_delivery_enabled"])
        # Revision 4 has no successor in the app's registry. Lifetime can still be
        # switched off for enrolled installs once enrollment is stopped.
        killed = {"CLOSABLE_FLOW_MEASUREMENT_REVISION": 4, "CLOSABLE_FLOW_LIFETIME_ENABLED": False}
        refreshed = self.assign(replay, **killed)
        self.assertEqual((refreshed["journey"], refreshed["lifetime_delivery_enabled"], refreshed["force_control_delivery"]),
                         (context, False, False))
        fresh = self.assign(self.request(installation=installation_in(policy.TEST)), **killed)
        self.assertEqual((fresh["journey"].assignment_source, fresh["journey"].randomized, fresh["lifetime_delivery_enabled"]),
                         ("server_disabled", False, False))
        # Resuming enrollment into that revision without Lifetime fails closed.
        with self.assertRaises(HTTPException) as resumed:
            self.assign(self.request(), CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, **killed)
        self.assertEqual(resumed.exception.status_code, 503)

    def test_context_registry_rejects_unknown_revisions_configs_and_cross_experiment_variants(self):
        context = self.enrolled(policy.TEST)
        base = context.model_dump()
        for changes in ({"measurement_revision": 5}, {"measurement_revision": 2},
                        {"config_version": "closable_flow_v9"}, {"config_version": "universal_onboarding_v5"},
                        {"variant": "search_first_standard"}, {"intended_paywall": "standard"},
                        {"exposure_id": f"activation_journey_2026_09:{context.installation_id}"},
                        {"measurement_revision": True}, {"measurement_revision": "1"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ActivationJourneyContext.model_validate({**base, **changes})
        valid = ActivationJourneyContext.model_validate({**base, "measurement_revision": 2, "config_version": "closable_flow_v2"})
        self.assertEqual(valid.config_version, "closable_flow_v2")
        old = {**base, "experiment_id": "activation_journey_2026_09", "variant": "search_first_standard",
               "exposure_id": f"activation_journey_2026_09:{context.installation_id}", "config_version": "draft_1",
               "intended_onboarding": "search_first", "intended_paywall": "standard"}
        ActivationJourneyContext.model_validate(old)
        for changes in ({"measurement_revision": 2}, {"variant": "closable_own_flight"}):
            with self.subTest(old=changes), self.assertRaises(ValidationError):
                ActivationJourneyContext.model_validate({**old, **changes})
        with self.assertRaises(ValidationError):
            ActivationJourneyContext.model_validate({**base, "assignment_source": "configuration_fallback",
                                                     "eligible": False, "randomized": False})

    def test_old_journey_canonical_json_is_byte_identical(self):
        stored = ('{"experiment_id":"activation_journey_2026_09","measurement_revision":1,"variant":"goals_flight_detail",'
                  '"eligible":true,"randomized":true,"installation_id":"11111111-2222-4333-8444-555555555555",'
                  '"exposure_id":"activation_journey_2026_09:11111111-2222-4333-8444-555555555555",'
                  '"enrollment_event_id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee","enrolled_at_ms":1789000000000,'
                  '"app_version":"3.9","build_number":"120","analytics_environment":"production",'
                  '"assignment_source":"server_assignment","config_version":"draft_1","intended_onboarding":"goals",'
                  '"intended_paywall":"flight_detail","goals_status":"required"}')
        self.assertEqual(canonical_context(ActivationJourneyContext.model_validate_json(stored)), stored)
        closable = self.enrolled(policy.CONTROL)
        self.assertEqual(list(json.loads(canonical_context(closable))), list(json.loads(stored)))

    def test_installation_belongs_to_one_journey_experiment_and_never_a_legacy_cohort(self):
        old_request = self.request()
        with patch.object(settings, "ACTIVATION_JOURNEY_PRODUCTION_ENROLLMENT_ENABLED", True):
            assignment(old_request, self.user, self.session)
        with self.assertRaises(HTTPException) as refused:
            self.assign(self.request(installation=old_request.installation_id), CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True)
        self.assertEqual(refused.exception.status_code, 409)
        closable = self.assign(self.request(), CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True)["journey"]
        with self.assertRaises(HTTPException) as reverse:
            assignment(self.request(installation=closable.installation_id), self.user, self.session)
        self.assertEqual(reverse.exception.status_code, 409)
        old_context = legacy.ExperimentReportingTests.context().model_copy(update={
            "installation_id": closable.installation_id, "analytics_environment": "production",
            "exposure_id": f"activation_experience_2026_08:{closable.installation_id}"})
        legacy_event = DiagnosticEvent(event_id=uuid4(), event_name="onboarding_started", occurred_at_ms=current_time_ms(),
            installation_id=closable.installation_id, app_version="3.10.0", build_number="146",
            analytics_environment="production", build_configuration="release", experiment=old_context,
            properties={"event_schema_version": 28})
        with self.assertRaises(HTTPException) as cohort:
            self.send(legacy_event)
        self.assertEqual(cohort.exception.status_code, 409)
        old = legacy.ExperimentReportingTests.context()
        legacy.report_experiment_exposure(old, self.user, self.session)
        with self.assertRaises(HTTPException) as existing:
            self.assign(self.request(installation=old.installation_id), CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True)
        self.assertEqual(existing.exception.status_code, 409)

    def test_canonical_exposure_enrolls_a_randomized_install_once(self):
        request = self.request(installation=installation_in(policy.TEST))
        context = self.assign(request, CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True)["journey"]
        self.send(self.event(context, "onboarding_started", properties={"step_key": "welcome_treatment"}))
        self.assertIsNone(self.session.get(ActivationJourneyEnrollment, context.exposure_id + ":v1"))
        exposure = self.exposure(context)
        self.assertEqual(self.send(exposure)["accepted"], 1)
        self.assertEqual(self.send(exposure)["duplicates"], 1)
        row = self.session.get(ActivationJourneyEnrollment, context.exposure_id + ":v1")
        self.assertEqual((row.experiment_id, row.measurement_revision, row.variant, row.randomized, row.config_version),
                         ("closable_flow_2026_10", 1, policy.TEST, True, "closable_flow_v1"))
        self.assertEqual(row.enrollment_event_id, str(context.enrollment_event_id))
        self.assertEqual(self.session.exec(select(ExperimentExposure)).all(), [])
        self.assertEqual(self.session.exec(select(ExperimentConversion)).all(), [])
        self.assertEqual(len(self.session.exec(select(ActivationJourneyEnrollment)).all()), 1)

    def test_exposure_must_be_canonical_and_old_milestones_are_rejected(self):
        context = self.assign(self.request(), CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True)["journey"]
        exposure = self.exposure(context).model_dump()
        for changes in ({"event_id": uuid4()}, {"occurred_at_ms": context.enrolled_at_ms + 1},
                        {"properties": {**exposure["properties"], "exposure_scope": "once_per_presentation"}},
                        {"properties": {**exposure["properties"], "exposure_scope": None}},
                        {"properties": {**exposure["properties"], "measurement_revision": 2}},
                        {"properties": {**exposure["properties"], "assignment_source": "server_disabled"}},
                        {"properties": {**exposure["properties"], "event_schema_version": 27}}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValidationError):
                DiagnosticEvent.model_validate({**exposure, **changes})
        for name in ("activation_journey_enrolled", "flight_detail_paywall_experiment_exposed"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.event(context, name, offset=0, identity=context.enrollment_event_id)
        with self.assertRaises(ValidationError):
            self.event(context, "af_search", schema=27)

    def test_nonrandomized_contexts_are_never_exposed_and_first_event_creates_excluded_entry(self):
        disabled = self.assign(self.request())["journey"]
        with self.assertRaises(ValidationError):
            self.exposure(disabled)
        events = [self.event(disabled, "onboarding_started", offset=offset) for offset in (1000, 2000)]
        self.send(events[0])
        self.send(events[1])
        rows = self.session.exec(select(ActivationJourneyEnrollment)).all()
        self.assertEqual([(row.assignment_source, row.randomized, row.variant) for row in rows],
                         [("server_disabled", False, policy.CONTROL)])
        fallback = ActivationJourneyContext.model_validate({
            **self.assign(self.request(), CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True)["journey"].model_dump(),
            "variant": policy.CONTROL, "eligible": False, "randomized": False, "assignment_source": "configuration_fallback",
            "intended_onboarding": "search_first", "intended_paywall": "hard"})
        with self.assertRaises(ValidationError):
            self.exposure(fallback)
        self.send(self.event(fallback, "onboarding_started"))
        entry = self.session.get(ActivationJourneyEnrollment, fallback.exposure_id + ":v1")
        self.assertEqual((entry.assignment_source, entry.randomized), ("configuration_fallback", False))
        # The client's frozen fallback wins over the late server proposal.
        proposal = self.session.get(ActivationJourneyAssignment, fallback.exposure_id + ":v1")
        late_context = ActivationJourneyContext.model_validate_json(proposal.context_json)
        with self.assertRaises(HTTPException) as late:
            self.send(self.event(late_context, "onboarding_started", offset=3000))
        self.assertEqual(late.exception.status_code, 409)
        # The old journey's REST enrollment never enrolls this experiment: only
        # the canonical exposure does.
        with self.assertRaises(HTTPException) as rest:
            enrollment(ActivationJourneyEnrollmentRequest(journey=late_context), self.user, self.session)
        self.assertEqual(rest.exception.status_code, 422)
        preview = ActivationJourneyContext.model_validate({
            **self.assign(self.request(environment="development"))["journey"].model_dump(),
            "assignment_source": "local_debug_preview"})
        self.send(self.exposure(preview))
        summary = closable_flow_summary(session=self.session, measurement_revision=1)
        self.assertEqual(sum(arm["installs"] for arm in summary["arms"].values()), 0)
        self.assertEqual(summary["quality"]["nonrandomized_enrollments_excluded"],
                         {"configuration_fallback": 1, "server_disabled": 1})

    def test_skip_flight_paywall_is_control_hard_paywall_only(self):
        control = self.enrolled(policy.CONTROL)
        test = self.enrolled(policy.TEST)
        skip = {"paywall_surface": "skip_flight", "effective_paywall": "hard", "effective_offer": "annual_weekly"}
        self.send(self.event(control, "paywall_viewed", properties=skip))
        for context, properties in ((test, skip), (control, {**skip, "effective_paywall": "closable_one_screen"}),
                                    (control, {**skip, "effective_offer": "annual_weekly_lifetime"}),
                                    (control, {**skip, "effective_offer": "standard", "effective_paywall": "standard"})):
            with self.subTest(properties=properties), self.assertRaises(ValidationError):
                self.event(context, "paywall_viewed", properties=properties)
        self.send(self.event(test, "paywall_viewed", properties={**skip, "operational_override": "forced_control"}))
        lifetime = self.enrolled(policy.CONTROL, CLOSABLE_FLOW_MEASUREMENT_REVISION=2, CLOSABLE_FLOW_LIFETIME_ENABLED=True)
        for offer in ("annual_weekly_lifetime", "annual_weekly"):
            self.send(self.event(lifetime, "paywall_viewed", properties={**skip, "effective_offer": offer}))

    def test_new_experience_actions_use_fixed_codes_and_identities(self):
        context = self.enrolled(policy.TEST)
        presentation = str(uuid4())
        for action in policy.EXPERIENCE_ACTIONS["paywall"]:
            self.send(self.event(context, "experience_action", properties={
                "screen": "paywall", "action": action, "experience_presentation_id": presentation, "source": "close"}))
        for action in policy.EXPERIENCE_ACTIONS["pending_flight"]:
            self.send(self.event(context, "experience_action", properties={
                "screen": "pending_flight", "action": action, "flight_identity": "a" * 64,
                "experience_presentation_id": presentation}))
        for properties in ({"screen": "paywall", "action": "viewed", "experience_presentation_id": presentation},
                           {"screen": "paywall", "action": "retry_panel_shown"},
                           {"screen": "pending_flight", "action": "track_tapped"},
                           {"screen": "pending_flight", "action": "card_viewed", "flight_identity": "a" * 64}):
            with self.subTest(properties=properties), self.assertRaises(ValidationError):
                self.event(context, "experience_action", properties=properties)
        # Existing screens keep their open action vocabulary.
        self.send(self.event(context, "experience_action", properties={"screen": "subscription", "action": "viewed"}))

    def test_new_value_lists_are_accepted_and_unknown_values_still_rejected(self):
        DiagnosticProperties(effective_onboarding="own_flight_preview", effective_paywall="closable_one_screen",
                             effective_offer="annual_weekly_lifetime", operational_override="lifetime_disabled")
        DiagnosticProperties(effective_paywall="hard", effective_offer="annual_weekly", operational_override="forced_control")
        for properties in ({"effective_offer": "universal"}, {"effective_paywall": "closable"},
                           {"operational_override": "forced_test"}, {"effective_onboarding": "welcome"},
                           {"arm": "test"}, {"lifetime_offered": "true"}):
            with self.subTest(properties=properties), self.assertRaises(ValidationError):
                DiagnosticProperties(**properties)

    def test_arm_and_lifetime_offered_match_the_frozen_randomized_context(self):
        # The app merges both into every schema-28 event of a randomized install.
        context = self.enrolled(policy.TEST)
        self.send(self.event(context, "paywall_products_loaded", properties={
            "arm": policy.TEST, "lifetime_offered": False, "status": "loaded"}))
        disabled = self.assign(self.request())["journey"]
        self.send(self.event(disabled, "paywall_viewed", properties={"lifetime_offered": False}))
        for journey, arm in ((context, policy.CONTROL), (disabled, policy.CONTROL)):
            self.event(journey, "af_search")
            with self.subTest(journey=journey.assignment_source, arm=arm), self.assertRaises(ValidationError):
                self.event(journey, "af_search", properties={"arm": arm})
        cohortless = {**self.event(context, "af_search").model_dump(), "journey": None}
        DiagnosticEvent.model_validate(cohortless)
        with self.assertRaises(ValidationError):
            DiagnosticEvent.model_validate({**cohortless, "properties": {"event_schema_version": 28, "arm": policy.TEST}})

    def test_departure_reminder_copy_is_not_retained(self):
        context = self.enrolled(policy.TEST)
        tap = {"source": "local", "notification_type": policy.DEPARTURE_REMINDER_NOTIFICATION, "action": "default_tap",
               "notification_id": "sofly_departure_offer_0123456789abcdef", "notification_copy_id": "b" * 64}
        content = {"title": "BA178 is scheduled to depart at 10:40", "body": "Gate changes and delays happen."}
        reminder = self.event(context, "push_opened", properties=tap).model_dump()
        self.send(DiagnosticEvent.model_validate({**reminder, "notification_content": content}))
        stored = json.loads(self.session.get(ExperimentDiagnosticEvent, str(reminder["event_id"])).properties_json)
        self.assertNotIn("_notification_copy_ciphertext", stored)
        self.assertNotIn("_notification_copy_digest", stored)
        self.assertEqual(stored["notification_copy_id"], "b" * 64)
        alert = self.event(context, "push_opened", properties={**tap, "notification_type": "gate_change"}).model_dump()
        self.send(DiagnosticEvent.model_validate({**alert, "notification_content": content}))
        self.assertIn("_notification_copy_ciphertext",
                      json.loads(self.session.get(ExperimentDiagnosticEvent, str(alert["event_id"])).properties_json))

    def purchase(self, context, *, transaction="closable-trial", original=None, product=policy.ANNUAL_PRODUCT_ID,
                 trial=True, price=0, owner=None, user=None, nonconsumable=False):
        decoded = legacy.ExperimentReportingTests.decoded_transaction(transaction)
        decoded.originalTransactionId = original or transaction
        decoded.productId = product
        decoded.purchaseDate = context.enrolled_at_ms + 60_000
        decoded.originalPurchaseDate = decoded.purchaseDate
        decoded.signedDate = decoded.purchaseDate + 100
        decoded.expiresDate = None if nonconsumable else decoded.purchaseDate + 7 * DAY
        decoded.price = price
        decoded.offerDiscountType = legacy.OfferDiscountType.FREE_TRIAL if trial else None
        decoded.appAccountToken = owner or self.user.id
        decoded.type = "Non-Consumable" if nonconsumable else "Auto-Renewable Subscription"
        request = CreateTransactionRequest(jws_payload="synthetic-signed-jws", journey=context)
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
            return create_or_update_transaction(request, user or self.user, self.session)

    def test_verified_purchase_is_attributed_and_restored_lifetime_never_is(self):
        context = self.enrolled(policy.TEST)
        self.assertEqual(self.purchase(context), {"detail": "successfull"})
        attribution = self.session.exec(select(ActivationJourneyAttribution)).one()
        self.assertEqual(attribution.enrollment_id, context.exposure_id + ":v1")
        lifetime = self.enrolled(policy.CONTROL, CLOSABLE_FLOW_MEASUREMENT_REVISION=2, CLOSABLE_FLOW_LIFETIME_ENABLED=True)
        self.assertEqual(self.purchase(lifetime, transaction="lifetime-1", product=policy.LIFETIME_PRODUCT_ID,
                                       trial=False, price=99_990, nonconsumable=True), {"detail": "successfull"})
        self.assertEqual(self.session.get(ActivationJourneyAttribution, "lifetime-1").enrollment_id, lifetime.exposure_id + ":v1")
        # The same Lifetime receipt restored on another new install and account.
        restorer = User(id="restoring-user")
        self.session.add(restorer)
        self.session.commit()
        self.user, original_user = restorer, self.user
        restored_context = self.enrolled(policy.TEST, CLOSABLE_FLOW_MEASUREMENT_REVISION=2, CLOSABLE_FLOW_LIFETIME_ENABLED=True)
        self.user = original_user
        result = self.purchase(restored_context, transaction="lifetime-1", product=policy.LIFETIME_PRODUCT_ID, trial=False,
                               price=99_990, nonconsumable=True, owner=original_user.id, user=restorer)
        self.assertEqual(result, {"detail": "successfull", "experiment_tracking_status": "conflict"})
        self.assertEqual(len(self.session.exec(select(ActivationJourneyAttribution)).all()), 2)
        self.assertIsNotNone(self.session.get(User, "restoring-user").premium_valid_until)
        self.assertIsNotNone(self.session.get(AppStoreRevenueEvent, "lifetime-1"))

    def test_routes_assignment_refresh_enrollment_and_protected_summary(self):
        app = FastAPI()
        app.include_router(journey_router, prefix="/subscriptions")
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_session] = lambda: self.session
        client = TestClient(app)
        payload = self.request(installation=installation_in(policy.TEST)).model_dump(mode="json")
        with self.switches(CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, CLOSABLE_FLOW_MEASUREMENT_REVISION=2,
                           CLOSABLE_FLOW_LIFETIME_ENABLED=True):
            first = client.post("/subscriptions/closable-flow/assignment", json=payload)
            second = client.post("/subscriptions/closable-flow/assignment", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        body = first.json()
        self.assertEqual(set(body), {"journey", "enrollment_enabled", "force_control_delivery",
                                     "lifetime_delivery_enabled", "operational_config_version"})
        self.assertEqual((body["journey"]["variant"], body["journey"]["measurement_revision"]), (policy.TEST, 2))
        response = client.post("/subscriptions/activation-journey/enrollment", json={"journey": body["journey"]})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.session.exec(select(ActivationJourneyEnrollment)).all(), [])
        self.send(self.exposure(ActivationJourneyContext.model_validate(body["journey"])))
        self.assertEqual(self.session.exec(select(ActivationJourneyEnrollment)).one().measurement_revision, 2)
        self.assertEqual(client.get("/subscriptions/closable-flow/summary?measurement_revision=2").status_code, 401)
        self.assertEqual(client.get("/subscriptions/closable-flow/summary?measurement_revision=2",
                                    headers={"Authorization": "Bearer wrong"}).status_code, 401)
        summary = client.get("/subscriptions/closable-flow/summary?measurement_revision=2",
                             headers={"Authorization": "Bearer test"})
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.json()["arms"][policy.TEST]["installs"], 1)
        self.assertNotIn(str(payload["installation_id"]), summary.text)
        invalid = client.get("/subscriptions/closable-flow/summary?measurement_revision=9", headers={"Authorization": "Bearer test"})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.session.exec(select(ActivationJourneyIdentity)).one().protocol, "journey")


if __name__ == "__main__":
    unittest.main()
