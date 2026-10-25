"""Matched onboarding / checkout, stable equal allocation, historical isolation."""
import json
from uuid import UUID
from pydantic import ValidationError
from tests.test_closable_flow_assignment import ClosableFlowAssignmentTests, policy, Settings
from core.activation_journey_contract import ActivationJourneyContext
from core.models.activation_journey import ActivationJourneyAssignment
from core.services.activation_journey import identity_key
from core.services.closable_flow_reporting import closable_flow_summary


class MatchedPaywallTests(ClosableFlowAssignmentTests):
    def test_matched_assignment_and_replay(self):
        seen = set()
        for index in range(1, 21):
            request = self.request(installation=UUID(int=index)).model_copy(update={"supports_matched_paywall": True})
            flags = dict(CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, CLOSABLE_FLOW_MEASUREMENT_REVISION=5)
            context = self.assign(request, **flags)["journey"]
            seen.add(context.variant)
            self.assertEqual(context.variant, policy.assigned_variant(request.installation_id, 50, 5))
            self.assertEqual(context.intended_onboarding, "own_flight_preview")
            self.assertEqual(context.measurement_revision, 5)
            replay = self.assign(request, **flags, CLOSABLE_FLOW_FORCE_CONTROL_DELIVERY=True)
            self.assertEqual(replay["journey"], context)
            self.assertTrue(replay["force_control_delivery"])
            # Canonical schema-29 exposure, idempotent installation denominator.
            event = self.exposure(context, event_schema_version=29)
            self.send(event)
            self.send(event)
            with self.assertRaises(ValidationError):
                self.exposure(context, event_schema_version=28)
            invalid = context.model_dump()
            invalid.update(measurement_revision=1, config_version="closable_flow_v1")
            with self.assertRaises(ValidationError):
                ActivationJourneyContext.model_validate(invalid)
        self.assertEqual(seen, {policy.MATCHED_CONTROL, policy.MATCHED_TEST})
        summary = closable_flow_summary(session=self.session, measurement_revision=5)
        self.assertEqual(set(summary["arms"]), seen)
        self.assertEqual(sum(arm["installs"] for arm in summary["arms"].values()), 20)
        historical = closable_flow_summary(session=self.session, measurement_revision=1)
        self.assertEqual(sum(arm["installs"] for arm in historical["arms"].values()), 0)

    def test_equal_split_and_settings(self):
        soft = sum(policy.assigned_variant(UUID(int=i), 50, 5) == policy.MATCHED_TEST for i in range(10000))
        self.assertTrue(4800 < soft < 5200, soft)
        for percent in (0, 49, 51, 100):
            with self.assertRaises(ValidationError):
                Settings(CLOSABLE_FLOW_MEASUREMENT_REVISION=5, CLOSABLE_FLOW_TEST_PERCENT=percent)

    def test_old_binary_and_existing_cohorts_stay_unchanged(self):
        request = self.request()
        old = self.assign(request, CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True)["journey"]
        refreshed = self.assign(request, CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True,
                                CLOSABLE_FLOW_MEASUREMENT_REVISION=5)["journey"]
        self.assertEqual(old, refreshed)
        disabled = self.assign(self.request(), CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True,
                               CLOSABLE_FLOW_MEASUREMENT_REVISION=5)["journey"]
        self.assertFalse(disabled.randomized)
        self.assertEqual((disabled.measurement_revision, disabled.variant), (1, policy.CONTROL))
        # Stored requests predating the optional capability field still replay.
        row = self.session.get(ActivationJourneyAssignment, identity_key(policy.EXPERIMENT_ID, request.installation_id))
        raw = json.loads(row.request_json)
        raw.pop("supports_matched_paywall", None)
        row.request_json = json.dumps(raw)
        self.session.add(row)
        self.session.commit()
        self.assertEqual(self.assign(request)["journey"], old)
