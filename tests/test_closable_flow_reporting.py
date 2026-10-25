"""Closable-flow report and non-purged funnel ledger; synthetic data only."""

import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from tests import test_experiment_reporting as legacy
from tests import test_closable_flow_assignment as assignment_tests

from sqlalchemy import delete
from sqlmodel import Session, SQLModel, create_engine, select

from core import closable_flow_policy as policy
from core import closable_flow_report as report
from core.activation_journey_contract import ActivationJourneyContext
from core.models.activation_journey import (
    ActivationJourneyAttribution, ActivationJourneyDiagnosticContext, ActivationJourneyEnrollment,
)
from core.models.apple_ads import AppStoreRevenueEvent
from core.models.closable_flow import ClosableFlowFunnelFact
from core.models.experiment import ExperimentDiagnosticEvent, current_time_ms
from core.models.subscription import Subscription
from core.models.subscription_lifecycle import AppStoreSubscriptionLifecycleEvent
from core.models.user import User
from core.routers.experiment_diagnostics import DiagnosticEvent
from core.routers.subscriptions import CreateTransactionRequest, create_or_update_transaction
from core.services import revenue_measurement
from core.services.closable_flow_funnel import refresh_closable_flow_funnel_facts
from core.services.closable_flow_reporting import closable_flow_summary
from scripts import report_closable_flow_test

DAY = 86_400_000
HOUR = 3_600_000
LIFETIME = {"CLOSABLE_FLOW_MEASUREMENT_REVISION": 2, "CLOSABLE_FLOW_LIFETIME_ENABLED": True}
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def fact(*, product=policy.ANNUAL_PRODUCT_ID, price=0, trial=True, purchase=1_000_000, revoked=None,
         percentage=None, reason="PURCHASE", original="original-1"):
    return {"id": f"{original}:{purchase}", "original_transaction_id": original, "product_id": product,
            "purchase_date_ms": purchase, "revoked_date_ms": revoked, "revocation_percentage": percentage,
            "transaction_reason": reason, "purchase_environment": "Production", "price_milliunits": price,
            "currency": "USD", "starts_trial": trial}


def lifecycle(kind, at, status=None, row_id=None):
    return {"id": row_id or f"{kind}:{at}", "original_transaction_id": "original-1", "event_kind": kind,
            "auto_renew_status": status, "notification_signed_date_ms": at}


class CommittedPayerAndStatisticsTests(unittest.TestCase):
    def test_committed_payer_components_follow_the_24_hour_rule(self):
        start = 1_000_000
        trial = fact(purchase=start)
        cases = [
            ("kept without lifecycle", trial, [], "kept_annual_trial"),
            ("disabled inside 24 h", trial, [lifecycle("auto_renew_disabled", start + HOUR)], None),
            ("disabled then enabled inside 24 h", trial, [lifecycle("auto_renew_disabled", start + HOUR),
                                                         lifecycle("auto_renew_enabled", start + 5 * HOUR)], "kept_annual_trial"),
            ("disabled after 24 h", trial, [lifecycle("auto_renew_disabled", start + DAY + 1)], "kept_annual_trial"),
            ("status change off", trial, [lifecycle("auto_renew_status_changed", start + HOUR, status=0)], None),
            ("refunded inside 24 h", fact(purchase=start, revoked=start + HOUR), [], None),
            ("refund lifecycle inside 24 h", trial, [lifecycle("refund", start + HOUR)], None),
            ("weekly", fact(product=policy.WEEKLY_PRODUCT_ID, price=9_990, trial=False, purchase=start), [], "weekly_purchase"),
            ("weekly refunded inside 24 h", fact(product=policy.WEEKLY_PRODUCT_ID, price=9_990, trial=False, purchase=start,
                                                  revoked=start + HOUR), [], None),
            ("weekly refunded later", fact(product=policy.WEEKLY_PRODUCT_ID, price=9_990, trial=False, purchase=start,
                                            revoked=start + 3 * DAY), [], "weekly_purchase"),
            ("lifetime", fact(product=policy.LIFETIME_PRODUCT_ID, price=99_990, trial=False, purchase=start), [], "lifetime_purchase"),
            ("lifetime v2", fact(product=policy.CURRENT_LIFETIME_PRODUCT_ID, price=149_990, trial=False, purchase=start), [], "lifetime_purchase"),
            ("annual without trial", fact(price=29_990, trial=False, purchase=start), [], "annual_purchase"),
            ("zero-price non-trial", fact(price=0, trial=False, purchase=start), [], None),
        ]
        for name, value, rows, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(report.committed_component(value, rows), expected)

    def test_refund_rule_matches_revenue_measurement(self):
        for price in (0, 1, 9_990, 99_990, 29_990):
            for percentage in (None, 0, 1, 33_333, 50_000, 100_000, 120_000):
                self.assertEqual(report.refunded_milliunits(price, percentage),
                                 revenue_measurement.refunded_milliunits(price, percentage))

    def test_beta_binomial_probability_is_exact(self):
        self.assertIsNone(report.probability_test_beats_control(1, 0, 1, 10))
        self.assertAlmostEqual(report.probability_test_beats_control(0, 10, 0, 10), 0.5, places=12)
        self.assertAlmostEqual(report.probability_test_beats_control(7, 400, 7, 400), 0.5, places=12)
        for x_t, n_t, x_c, n_c in ((3, 4, 1, 2), (12, 400, 6, 400), (30, 410, 20, 395), (0, 50, 4, 60)):
            with self.subTest(case=(x_t, n_t, x_c, n_c)):
                exact = report.probability_test_beats_control(x_t, n_t, x_c, n_c)
                reverse = report.probability_test_beats_control(x_c, n_c, x_t, n_t)
                self.assertAlmostEqual(exact + reverse, 1.0, places=9)
                generator = random.Random(20261021)
                draws = 200_000
                wins = sum(generator.betavariate(1 + x_t, 1 + n_t - x_t) > generator.betavariate(1 + x_c, 1 + n_c - x_c)
                           for _ in range(draws))
                self.assertAlmostEqual(exact, wins / draws, delta=0.006)


class ClosableFlowReportingTests(unittest.TestCase):
    setUp = legacy.ExperimentReportingTests.setUp
    tearDown = legacy.ExperimentReportingTests.tearDown
    switches = assignment_tests.ClosableFlowAssignmentTests.switches
    request = assignment_tests.ClosableFlowAssignmentTests.request
    event = assignment_tests.ClosableFlowAssignmentTests.event
    exposure = assignment_tests.ClosableFlowAssignmentTests.exposure
    send = assignment_tests.ClosableFlowAssignmentTests.send

    def install(self, arm, enrolled, *, expose=True):
        user = User(id=str(uuid4()))
        self.session.add(user)
        self.session.commit()
        request = self.request(installation=assignment_tests.installation_in(arm), enrolled=enrolled)
        with self.switches(CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED=True, **LIFETIME), \
             patch("core.services.activation_journey.current_time_ms", return_value=enrolled + 1000):
            from core.routers.activation_journey import closable_flow_assignment
            context = closable_flow_assignment(request, user, self.session)["journey"]
        if expose:
            self.send(self.exposure(context), user=user)
        return context, user

    def purchase(self, context, user, transaction, *, product=policy.ANNUAL_PRODUCT_ID, price=0, trial=True,
                 offset=60_000, original=None, reason="PURCHASE", revoked=None, signed_offset=100):
        decoded = legacy.ExperimentReportingTests.decoded_transaction(transaction)
        decoded.originalTransactionId = original or transaction
        decoded.productId = product
        decoded.purchaseDate = context.enrolled_at_ms + offset
        decoded.originalPurchaseDate = context.enrolled_at_ms + 60_000
        decoded.signedDate = decoded.purchaseDate + signed_offset
        lifetime = product == policy.LIFETIME_PRODUCT_ID
        decoded.expiresDate = None if lifetime else decoded.purchaseDate + 7 * DAY
        decoded.price = price
        decoded.offerDiscountType = legacy.OfferDiscountType.FREE_TRIAL if trial else None
        decoded.transactionReason = reason
        decoded.revocationDate = revoked
        decoded.appAccountToken = user.id
        decoded.type = "Non-Consumable" if lifetime else "Auto-Renewable Subscription"
        request = CreateTransactionRequest(jws_payload="synthetic-signed-jws", journey=context)
        with patch("core.routers.subscriptions.AppStoreService.process_transaction", return_value=decoded):
            self.assertEqual(create_or_update_transaction(request, user, self.session), {"detail": "successfull"})
        return decoded

    def renewal_change(self, original, kind, at):
        subtype = {"auto_renew_disabled": "AUTO_RENEW_DISABLED", "auto_renew_enabled": "AUTO_RENEW_ENABLED"}[kind]
        self.session.add(AppStoreSubscriptionLifecycleEvent(
            id=str(uuid4()), notification_type="DID_CHANGE_RENEWAL_STATUS", subtype=subtype, event_kind=kind,
            original_transaction_id=original, purchase_environment="Production", notification_signed_date_ms=at))
        self.session.commit()

    def paywall(self, context, user, presentation, at, source, *, paywall="closable_one_screen"):
        self.send(self.event(context, "paywall_viewed", offset=at, presentation=presentation, properties={
            "source": source, "effective_paywall": paywall, "effective_offer": "annual_weekly_lifetime",
            "operational_override": "none", "layout": paywall, "paywall_surface": "selected_flight",
            "flight_identity": "b" * 64}), user=user)

    def checkout(self, context, user, presentation, at, outcome):
        attempt = uuid4()
        self.send(self.event(context, "af_initiated_checkout", offset=at, presentation=presentation, attempt=attempt,
                             properties={"product_id": policy.WEEKLY_PRODUCT_ID}),
                  self.event(context, "checkout_attempt_completed", offset=at + 5_000, presentation=presentation,
                             attempt=attempt, properties={"outcome": outcome}), user=user)

    def test_end_to_end_summary_per_arm_and_after_diagnostics_purge(self):
        now = current_time_ms()
        base = now - 40 * DAY
        control_a, user_a = self.install(policy.CONTROL, base)
        control_b, user_b = self.install(policy.CONTROL, base + HOUR)
        test_c, user_c = self.install(policy.TEST, base)
        test_d, user_d = self.install(policy.TEST, base)
        test_e, user_e = self.install(policy.TEST, base)
        test_f, user_f = self.install(policy.TEST, base)
        self.install(policy.TEST, base, expose=False)  # assigned, exposure never arrived
        disabled_user = User(id=str(uuid4()))
        self.session.add(disabled_user)
        self.session.commit()
        with self.switches(**LIFETIME), patch("core.services.activation_journey.current_time_ms", return_value=base + 1000):
            from core.routers.activation_journey import closable_flow_assignment
            closable_flow_assignment(self.request(enrolled=base), disabled_user, self.session)

        self.purchase(control_a, user_a, "a-trial")
        self.purchase(control_a, user_a, "a-renewal", original="a-trial", price=29_990, trial=False,
                      offset=7 * DAY + 60_000, reason="RENEWAL")
        weekly = self.purchase(test_c, user_c, "c-weekly", product=policy.WEEKLY_PRODUCT_ID, price=9_990, trial=False,
                               offset=HOUR + 20_000)
        self.purchase(test_c, user_c, "c-weekly", product=policy.WEEKLY_PRODUCT_ID, price=9_990, trial=False,
                      offset=HOUR + 20_000, revoked=weekly.purchaseDate + 3 * DAY, signed_offset=3 * DAY + 1)
        self.purchase(test_d, user_d, "d-lifetime", product=policy.LIFETIME_PRODUCT_ID, price=99_990, trial=False)
        trial_e = self.purchase(test_e, user_e, "e-trial")
        self.renewal_change("e-trial", "auto_renew_disabled", trial_e.purchaseDate + 2 * HOUR)
        trial_f = self.purchase(test_f, user_f, "f-trial")
        self.renewal_change("f-trial", "auto_renew_disabled", trial_f.purchaseDate + 10 * 60_000)
        self.renewal_change("f-trial", "auto_renew_enabled", trial_f.purchaseDate + 5 * HOUR)
        # Install B's account already owned a subscription before enrolling (reinstall).
        self.session.add(Subscription(id="b-old"))
        self.session.get(User, user_b.id).subscriptions.append(self.session.get(Subscription, "b-old"))
        self.session.add(AppStoreRevenueEvent(id="b-old-tx", original_transaction_id="b-old", user_id=user_b.id,
            product_id=policy.WEEKLY_PRODUCT_ID, purchase_date_ms=base - 10 * DAY, purchase_environment="Production",
            price_milliunits=9_990, currency="USD"))
        self.session.commit()

        p1, p2, p3, p4, p5 = (uuid4() for _ in range(5))
        self.paywall(test_c, user_c, p1, 10_000, "universal_onboarding")
        self.send(self.event(test_c, "paywall_dismissed", offset=20_000, presentation=p1, properties={"source": "close"}), user=user_c)
        for action, at in (("reminder_offer_shown", 25_000), ("reminder_opted_in", 26_000)):
            self.send(self.event(test_c, "experience_action", offset=at, properties={
                "screen": "pending_flight", "action": action, "flight_identity": "b" * 64,
                "experience_presentation_id": str(uuid4())}), user=user_c)
        self.paywall(test_c, user_c, p2, HOUR, "paywall_gated_pending_flight")
        self.checkout(test_c, user_c, p2, HOUR + 2_000, "cancelled")
        panel = str(uuid4())
        for action, at in (("retry_panel_shown", HOUR + 8_000), ("retry_checkout_tapped", HOUR + 10_000)):
            self.send(self.event(test_c, "experience_action", offset=at, presentation=p2, properties={
                "screen": "paywall", "action": action, "experience_presentation_id": panel}), user=user_c)
        self.checkout(test_c, user_c, p2, HOUR + 11_000, "verified")
        self.send(self.event(test_c, "push_opened", offset=2 * HOUR, properties={
            "source": "local", "notification_type": "departure_paywall_reminder", "action": "default_tap"}), user=user_c)
        self.paywall(test_c, user_c, p3, 2 * HOUR + 1_000, "paywall_gated_departure_reminder")
        self.paywall(test_d, user_d, p4, 10_000, "universal_onboarding")
        self.checkout(test_d, user_d, p4, 15_000, "verified")
        self.paywall(control_a, user_a, p5, 10_000, "universal_onboarding", paywall="hard")
        self.send(self.event(control_a, "review_prompt_requested", offset=5_000, properties={"source": "onboarding_sharing"}), user=user_a)

        live = closable_flow_summary(session=self.session, measurement_revision=2)
        self.assertEqual(live["arms"][policy.TEST]["paywall"]["coverage"]["installs_derived_live_from_retained_diagnostics"], 4)
        counts = refresh_closable_flow_funnel_facts(session=self.session, now_ms=now)
        self.session.commit()
        self.assertEqual(counts, {"finalized": 6, "scanned": 6, "written": 6})

        def check(summary):
            control, test = summary["arms"][policy.CONTROL], summary["arms"][policy.TEST]
            self.assertEqual((control["installs"], test["installs"]), (2, 4))
            self.assertEqual((control["committed_payers"]["d7"]["numerator"], test["committed_payers"]["d7"]["numerator"]), (1, 3))
            self.assertEqual(test["committed_payers"]["d7"]["installs_by_component"],
                             {"kept_annual_trial": 1, "weekly_purchase": 1, "lifetime_purchase": 1, "annual_purchase": 0})
            self.assertEqual(test["committed_payers"]["d7"]["kept_trial_share"]["rate"], 0.5)
            self.assertEqual(test["committed_payers"]["d7"]["trials_auto_renew_off_within_1h"]["numerator"], 1)
            self.assertEqual(test["committed_payers"]["d7"]["trial_lifecycle_coverage"]["numerator"], 2)
            self.assertEqual((control["verified_trial_starts"]["d1"]["numerator"], test["verified_trial_starts"]["d7"]["numerator"]), (1, 2))
            self.assertEqual(summary["decision"]["p_test_beats_control"], report.probability_test_beats_control(3, 4, 1, 2))
            self.assertEqual(summary["decision"]["primary_metric"], "committed_payers_d7")
            immediate = test["immediate_net_revenue"]["d7"]["by_currency"]
            self.assertEqual(immediate, [{"currency": "USD", "gross_milliunits": 109_980, "refund_milliunits": 9_990,
                                          "transactions": 2, "net_milliunits": 99_990, "net_per_install_milliunits": 99_990 / 4}])
            self.assertEqual(control["immediate_net_revenue"]["d7"]["by_currency"], [])
            self.assertEqual(test["lifetime_uptake"]["d7"]["numerator"], 1)
            self.assertEqual(summary["overall"]["lifetime_uptake_d7"]["numerator"], 1)
            self.assertEqual(summary["overall"]["lifetime_uptake_d7"]["denominator"], 6)
            self.assertEqual((control["paid_per_install"]["d14"]["numerator"], test["paid_per_install"]["d35"]["numerator"]), (1, 1))
            self.assertEqual(control["net_revenue"]["d35"]["by_currency"][0]["net_milliunits"], 29_990)
            self.assertEqual(test["net_revenue"]["d35"]["by_currency"][0]["net_milliunits"], 99_990)
            paywall = test["paywall"]
            self.assertEqual(paywall["presentations"], 4)
            self.assertEqual(paywall["presentations_by_source"], {"paywall_gated_departure_reminder": 1,
                                                                  "paywall_gated_pending_flight": 1, "universal_onboarding": 2})
            self.assertEqual((paywall["close_rate_per_presentation"]["numerator"], paywall["close_rate_per_presentation"]["denominator"]), (1, 4))
            self.assertEqual(paywall["close_rate_per_reaching_install"]["rate"], 0.5)
            self.assertEqual(paywall["reach_24h"]["numerator"], 2)
            self.assertEqual(paywall["return_within_7d_after_close"]["rate"], 1.0)
            self.assertEqual(paywall["first_return_sources"], {"paywall_gated_pending_flight": 1})
            self.assertAlmostEqual(paywall["median_hours_to_return"], (HOUR - 20_000) / HOUR)
            self.assertEqual(paywall["installs_converting_on_return"], 1)
            self.assertEqual(paywall["retry_funnel_installs"], {"panel_shown": 1, "retry_tapped": 1, "verified_after_retry": 1})
            self.assertEqual(paywall["reminder_funnel_installs"], {"offer_shown": 1, "opted_in": 1, "push_opened": 1,
                                                                   "reminder_paywall": 1, "verified": 0})
            self.assertEqual(paywall["checkout_attempt_outcomes"], {"cancelled": 1, "verified": 2})
            self.assertEqual(control["paywall"]["close_rate_per_presentation"]["numerator"], 0)
            self.assertEqual((control["onboarding_rating_prompts"], test["onboarding_rating_prompts"]), (1, 0))
            self.assertEqual(control["installs_with_pre_enrollment_purchase"], 1)
            quality = summary["quality"]
            self.assertEqual(quality["server_assignments_without_enrollment_after_24h"], {policy.CONTROL: 0, policy.TEST: 1})
            self.assertEqual(quality["daily_assignment_sources"][0]["server_disabled"], 1)
            self.assertEqual(quality["mismatched_financial_facts_excluded"], 0)
            self.assertEqual(quality["lifetime_offered_outside_lifetime_revision"], 0)
            text = json.dumps(summary)
            for context, user in ((control_a, user_a), (test_c, user_c), (test_d, user_d)):
                self.assertNotIn(str(context.installation_id), text)
                self.assertNotIn(user.id, text)
            for transaction in ("a-trial", "c-weekly", "d-lifetime"):
                self.assertNotIn(transaction, text)

        check(live)
        ledger = closable_flow_summary(session=self.session, measurement_revision=2)
        check(ledger)
        self.assertEqual(ledger["arms"][policy.TEST]["paywall"]["coverage"]["installs_with_ledger_fact"], 4)
        self.assertEqual(ledger["arms"][policy.TEST]["paywall"], {**live["arms"][policy.TEST]["paywall"],
                                                                  "coverage": ledger["arms"][policy.TEST]["paywall"]["coverage"]})
        self.assertEqual(closable_flow_summary(session=self.session, measurement_revision=1)["arms"][policy.TEST]["installs"], 0)
        # The 90-day purge removes the source diagnostics; the funnel survives.
        self.session.exec(delete(ActivationJourneyDiagnosticContext))
        self.session.exec(delete(ExperimentDiagnosticEvent))
        self.session.commit()
        self.assertEqual(refresh_closable_flow_funnel_facts(session=self.session, now_ms=now), {"scanned": 0})
        check(closable_flow_summary(session=self.session, measurement_revision=2))

    def test_funnel_facts_never_regress_and_finalize_after_the_horizon(self):
        now = current_time_ms()
        context, user = self.install(policy.TEST, now - 3 * DAY)
        presentation = uuid4()
        self.paywall(context, user, presentation, 10_000, "universal_onboarding")
        self.send(self.event(context, "paywall_dismissed", offset=20_000, presentation=presentation,
                             properties={"source": "close"}), user=user)
        self.assertEqual(refresh_closable_flow_funnel_facts(session=self.session, now_ms=now), {"scanned": 1, "written": 1})
        row = self.session.get(ClosableFlowFunnelFact, context.exposure_id + ":v1")
        self.assertEqual((row.final, row.close_presentations, row.source_event_count, row.retention_complete), (False, 1, 3, True))
        self.session.exec(delete(ActivationJourneyDiagnosticContext))
        self.session.exec(delete(ExperimentDiagnosticEvent))
        self.session.commit()
        self.assertEqual(refresh_closable_flow_funnel_facts(session=self.session, now_ms=now + DAY),
                         {"kept_previous_after_regression": 1, "scanned": 1})
        later = context.enrolled_at_ms + 38 * DAY
        self.assertEqual(refresh_closable_flow_funnel_facts(session=self.session, now_ms=later),
                         {"finalized": 1, "kept_previous_after_regression": 1, "scanned": 1})
        self.session.commit()
        row = self.session.get(ClosableFlowFunnelFact, context.exposure_id + ":v1", populate_existing=True)
        self.assertEqual((row.final, row.close_presentations), (True, 1))
        self.assertEqual(refresh_closable_flow_funnel_facts(session=self.session, now_ms=later + DAY), {"scanned": 0})

    def test_unknown_protocol_counts_cohortless_schema_28_onboarding_starts(self):
        now = current_time_ms()
        context, user = self.install(policy.TEST, now - 2 * HOUR)
        self.send(self.event(context, "onboarding_started"), user=user)
        # No context could be saved: no journey, no legacy cohort, schema 28.
        self.send(*[DiagnosticEvent(
            event_id=uuid4(), event_name="onboarding_started", occurred_at_ms=now - HOUR, installation_id=uuid4(),
            app_version="3.10.0", build_number="146", analytics_environment="production", build_configuration="release",
            properties={"event_schema_version": schema}) for schema in (28, 28, 27)])
        quality = closable_flow_summary(session=self.session, measurement_revision=2)["quality"]
        self.assertEqual(quality["unknown_protocol_installs"], 2)
        self.assertEqual(sum(day["unknown_protocol_onboarding_installs"] for day in quality["daily_assignment_sources"]), 2)
        self.assertEqual(closable_flow_summary(session=self.session, measurement_revision=2,
                                               analytics_environment="testflight")["quality"]["unknown_protocol_installs"], 0)

    def test_client_fallbacks_count_in_daily_health_whatever_their_revision(self):
        now = current_time_ms()
        self.install(policy.TEST, now - 2 * HOUR)  # a revision-2 server assignment
        installation = uuid4()
        # The app cannot know the server's revision, so its fallback says 1.
        fallback = ActivationJourneyContext(
            experiment_id=policy.EXPERIMENT_ID, measurement_revision=1, variant=policy.CONTROL, eligible=False,
            randomized=False, installation_id=installation, exposure_id=f"{policy.EXPERIMENT_ID}:{installation}",
            enrollment_event_id=uuid4(), enrolled_at_ms=now - 2 * HOUR, app_version="3.10.0", build_number="146",
            analytics_environment="production", assignment_source="configuration_fallback",
            config_version="closable_flow_v1", intended_onboarding="search_first", intended_paywall="hard",
            goals_status="not_asked")
        self.send(self.event(fallback, "onboarding_started"))
        for revision, excluded in ((1, {"configuration_fallback": 1}), (2, {})):
            quality = closable_flow_summary(session=self.session, measurement_revision=revision)["quality"]
            daily = quality["daily_assignment_sources"]
            self.assertEqual((sum(day["server_assignments"] for day in daily), sum(day["configuration_fallback"] for day in daily)),
                             (1, 1))
            self.assertEqual(quality["nonrandomized_enrollments_excluded"], excluded)

    def test_derived_funnel_ignores_events_outside_the_horizon(self):
        enrolled = 1_000_000
        events = [
            {"event_name": "paywall_viewed", "occurred_at_ms": enrolled + 10, "paywall_presentation_id": "p1",
             "checkout_attempt_id": None, "properties": {"source": "universal_onboarding"}},
            {"event_name": "paywall_viewed", "occurred_at_ms": enrolled - 10, "paywall_presentation_id": "p0",
             "checkout_attempt_id": None, "properties": {"source": "early"}},
            {"event_name": "paywall_viewed", "occurred_at_ms": enrolled + 36 * DAY, "paywall_presentation_id": "p9",
             "checkout_attempt_id": None, "properties": {"source": "late"}},
            {"event_name": "checkout_attempt_completed", "occurred_at_ms": enrolled + 20, "paywall_presentation_id": "p1",
             "checkout_attempt_id": "a1", "properties": {"outcome": "pending"}},
            {"event_name": "checkout_attempt_completed", "occurred_at_ms": enrolled + 30, "paywall_presentation_id": "p1",
             "checkout_attempt_id": "a1", "properties": {"outcome": "verified"}},
        ]
        derived = report.derive_funnel_fact(events, enrolled_at_ms=enrolled, horizon_ms=35 * DAY, through_ms=enrolled + 40 * DAY)
        self.assertEqual((derived["paywall_presentations"], derived["source_event_count"]), (1, 3))
        self.assertEqual(json.loads(derived["terminal_outcomes_json"]), {"conflicting": 1})
        self.assertEqual(derived["verified_first_presentation_attempts"], 0)


class ReadOnlyReportScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "report.db"
        engine = create_engine(f"sqlite:///{self.database}")
        SQLModel.metadata.create_all(engine)
        self.now = current_time_ms()
        enrolled = self.now - 20 * DAY
        self.secrets = []
        with Session(engine) as session:
            for number, (variant, product, price, trial) in enumerate((
                (policy.CONTROL, policy.ANNUAL_PRODUCT_ID, 0, True), (policy.CONTROL, None, 0, False),
                (policy.TEST, policy.WEEKLY_PRODUCT_ID, 9_990, False), (policy.TEST, policy.LIFETIME_PRODUCT_ID, 99_990, False),
            )):
                installation, user = str(uuid4()), str(uuid4())
                entry = f"closable_flow_2026_10:{installation}:v1"
                context = json.dumps({"variant": variant, "number": number})
                self.secrets += [installation, user]
                session.add(User(id=user))
                session.add(ActivationJourneyEnrollment(
                    id=entry, experiment_id=policy.EXPERIMENT_ID, measurement_revision=1, installation_id=installation,
                    user_id=user, variant=variant, eligible=True, randomized=True, app_version="3.10.0",
                    build_number="146", analytics_environment="production", enrolled_at_ms=enrolled,
                    enrollment_event_id=str(uuid4()), assignment_source="server_assignment",
                    config_version="closable_flow_v1", context_json=context, first_reported_at_ms=enrolled))
                if product is None:
                    continue
                transaction = f"secret-transaction-{uuid4()}"
                self.secrets.append(transaction)
                session.add(ActivationJourneyAttribution(id=transaction, enrollment_id=entry, user_id=user,
                                                         first_transaction_id=transaction, context_json=context,
                                                         attributed_at_ms=enrolled + 1_000))
                session.add(AppStoreRevenueEvent(id=transaction, original_transaction_id=transaction, user_id=user,
                    product_id=product, purchase_date_ms=enrolled + 60_000, purchase_environment="Production",
                    price_milliunits=price, currency="USD", starts_trial=trial, transaction_reason="PURCHASE"))
            session.commit()
        engine.dispose()

    def tearDown(self):
        self.directory.cleanup()

    def digest(self):
        return hashlib.sha256(self.database.read_bytes()).hexdigest()

    def test_script_is_read_only_and_reports_aggregates_only(self):
        before = self.digest()
        result = report_closable_flow_test.run(["--database", str(self.database), "--revision", "1"], now_ms=self.now)
        self.assertEqual(self.digest(), before)
        self.assertEqual(sorted(path.name for path in Path(self.directory.name).iterdir()), ["report.db"])
        arms = result["arms"]
        self.assertEqual((arms[policy.CONTROL]["installs"], arms[policy.TEST]["installs"]), (2, 2))
        self.assertEqual((arms[policy.CONTROL]["committed_payers"]["d7"]["numerator"],
                          arms[policy.TEST]["committed_payers"]["d7"]["numerator"]), (1, 2))
        self.assertEqual(arms[policy.TEST]["paid_per_install"]["d14"]["numerator"], 2)
        self.assertEqual(arms[policy.TEST]["paid_per_install"]["d35"], {"status": "not_mature", "censored_installs": 2})
        self.assertEqual(result["decision"]["p_test_beats_control"], report.probability_test_beats_control(2, 2, 1, 2))
        self.assertTrue(result["quality"]["funnel_fact_table_present"])
        text = json.dumps(result)
        for secret in self.secrets:
            self.assertNotIn(secret, text)
        self.assertEqual(report_closable_flow_test.run(["--database", str(self.database), "--revision", "9"], now_ms=self.now),
                         {"error": "invalid_report_window_revision_or_fx_table"})
        rates = Path(self.directory.name) / "fx.json"
        rates.write_text(json.dumps({"USD": 1.0}))
        converted = report_closable_flow_test.run(["--database", str(self.database), "--revision", "1",
                                                   "--usd-rates", str(rates), "--proceeds-ratio", "0.7"], now_ms=self.now)
        estimate = converted["arms"][policy.TEST]["immediate_net_revenue"]["d7"]["usd_estimate"]
        self.assertAlmostEqual(estimate["customer_net_usd_per_install"], (9.99 + 99.99) / 2)
        self.assertAlmostEqual(estimate["estimated_proceeds_usd_per_install"], 0.7 * (9.99 + 99.99) / 2)
        self.assertTrue(estimate["complete"])
        self.assertIsNone(result["arms"][policy.TEST]["immediate_net_revenue"]["d7"]["usd_estimate"])
        rates.write_text(json.dumps({"USD": -1}))
        self.assertIn("error", report_closable_flow_test.run(["--database", str(self.database), "--revision", "1",
                                                              "--usd-rates", str(rates)], now_ms=self.now))
        rates.unlink()
        self.assertEqual(report_closable_flow_test.run(["--database", str(self.database) + ".missing", "--revision", "1"]),
                         {"error": "database_unavailable_or_schema_missing"})

    def test_script_runs_without_application_configuration_or_funnel_table(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DROP TABLE closableflowfunnelfact")
        before = self.digest()
        completed = subprocess.run(
            [sys.executable, "-I", str(REPOSITORY_ROOT / "scripts/report_closable_flow_test.py"),
             "--database", str(self.database), "--revision", "1"],
            capture_output=True, text=True, timeout=60, cwd=self.directory.name,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        result = json.loads(completed.stdout)
        self.assertFalse(result["quality"]["funnel_fact_table_present"])
        self.assertEqual(result["arms"][policy.TEST]["installs"], 2)
        self.assertEqual(self.digest(), before)
        self.assertEqual(sorted(path.name for path in Path(self.directory.name).iterdir()), ["report.db"])


if __name__ == "__main__":
    unittest.main()
