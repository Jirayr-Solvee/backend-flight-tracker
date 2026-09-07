"""3.8 reliability contracts, including independent SQLite writer sessions."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4
from unittest.mock import AsyncMock, patch

from tests import test_experiment_reporting as goals
from tests import test_experiment_revision2 as reports
from tests import test_search_recovery as search
from tests import test_search_failure_reporting as search_reports

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine, select

from core.config import settings
from core.models import get_session
from core.models.experiment import (
    ExperimentExposure, ExperimentGoalConfirmation, ExperimentGoalConfirmationReceipt,
    ExperimentGoalSelection,
)
from core.models.flight import QuerySearchResponse
from core.models.flight import SearchQueryRequest
from core.models.user import User
from core.routers.subscriptions import (
    ExperimentGoalSelectionRequest, report_experiment_goal_selection, router,
)
from core.routers.flights import _execute_search_with_date_fallback_details
from core.routers.flights import search_flights_from_text_post
from core.services.flight.api_client import AerodataboxUnavailableError
from core.services.flight.service import FlightQueryHandler
from core.services.gemini.service import GeminiService, ResolvedFunctionCall


class GoalConfirmationTests(unittest.TestCase):
    setUp = goals.ExperimentReportingTests.setUp
    tearDown = goals.ExperimentReportingTests.tearDown
    context = staticmethod(goals.ExperimentReportingTests.context)

    def request(self, revision=1, keys=None, timestamp=1000, context=None):
        return ExperimentGoalSelectionRequest(
            experiment=context or self.context(),
            selected_goal_keys=keys or ["family_friends"], selected_at_ms=timestamp,
            confirmation_revision=revision,
            confirmation_id=uuid4() if revision is not None else None,
        )

    def send(self, data):
        return report_experiment_goal_selection(data, self.user, self.session)

    def stored(self):
        self.session.expire_all()
        return self.session.get(ExperimentGoalSelection, self.context().exposure_id)

    def test_monotonic_revision_wins_even_when_capture_clock_moves_backwards(self):
        first = self.request(1, timestamp=5000)
        newest = self.request(3, ["flight_history"], timestamp=500)
        middle = self.request(2, ["copilot_insights"], timestamp=4000)
        self.assertEqual(self.send(first)["status"], "accepted")
        self.assertEqual(self.send(newest)["status"], "accepted")
        stale = self.send(middle)
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["accepted_revision"], 3)
        self.assertEqual(stale["request_confirmation_id"], str(middle.confirmation_id))
        self.assertEqual(stale["accepted_confirmation_id"], str(newest.confirmation_id))
        self.assertEqual(self.stored().selected_goal_keys, "flight_history")
        self.assertEqual(self.stored().selected_at_ms, 500)

    def test_a_b_a_and_lost_ack_exact_retry(self):
        a1 = self.request(1)
        b = self.request(2, ["flight_history"])
        a3 = self.request(3)
        for data in (a1, b, a3):
            self.send(data)
        self.assertEqual(self.send(a3)["status"], "idempotent")
        self.assertEqual(self.send(a1)["status"], "stale")
        self.assertEqual(self.stored().selected_goal_keys, "family_friends")
        self.assertEqual(len(self.session.exec(select(ExperimentGoalSelection)).all()), 1)

    def test_equal_conflicting_identity_payload_and_reused_identity_rejected(self):
        accepted = self.request()
        self.send(accepted)
        for conflict in (
            self.request(keys=["flight_history"]),
            accepted.model_copy(update={"selected_goal_keys": ["flight_history"]}),
            accepted.model_copy(update={"confirmation_revision": 2}),
            accepted.model_copy(update={"experiment": self.context().model_copy(update={"build_number": "999"})}),
        ):
            with self.subTest(conflict=conflict.confirmation_revision):
                with self.assertRaises(HTTPException) as caught:
                    self.send(conflict)
                self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.stored().selected_goal_keys, "family_friends")

    def test_legacy_ordering_and_versioned_upgrade_prevent_old_client_overwrite(self):
        old = self.request(None, ["flight_history"], timestamp=2000)
        self.assertEqual(self.send(old), {"detail": "success"})
        self.send(self.request(None, ["copilot_insights"], timestamp=1000))
        self.assertEqual(self.stored().selected_goal_keys, "flight_history")
        self.assertEqual(self.send(old), {"detail": "success"})
        with self.assertRaises(HTTPException) as caught:
            self.send(self.request(None, timestamp=2000))
        self.assertEqual(caught.exception.status_code, 409)
        self.send(self.request(1, timestamp=100))
        self.send(self.request(None, ["flight_history"], timestamp=9000))
        self.assertEqual(self.stored().selected_goal_keys, "family_friends")

    def test_existing_legacy_row_is_seeded_without_retroactively_new_metadata(self):
        context = self.context()
        self.session.add(ExperimentGoalSelection(
            id=context.exposure_id, exposure_id=context.exposure_id,
            experiment_id=context.experiment_id, variant=context.variant,
            eligible=True, installation_id=str(context.installation_id),
            app_version="3.7", build_number="118", analytics_environment="production",
            user_id=self.user.id, selected_goal_keys="flight_history", selected_at_ms=2000,
        ))
        self.session.commit()
        self.send(self.request(None, timestamp=1000))
        self.assertEqual(self.stored().selected_goal_keys, "flight_history")
        self.assertEqual(self.stored().app_version, "3.7")

    def test_capture_context_survives_retry_and_environment_conflict_rolls_back(self):
        context = self.context().model_copy(update={"app_version": "3.8", "build_number": "119"})
        data = self.request(context=context)
        self.send(data)
        self.send(data)
        ledger = self.session.get(ExperimentGoalConfirmation, context.exposure_id)
        self.assertEqual(json.loads(ledger.capture_payload_json)["experiment"]["build_number"], "119")
        conflict = self.request(2, context=context.model_copy(update={"analytics_environment": "testflight"}))
        with self.assertRaises(HTTPException) as caught:
            self.send(conflict)
        self.assertEqual(caught.exception.status_code, 409)
        self.session.refresh(ledger)
        self.assertEqual(ledger.confirmation_revision, 1)

    def test_paired_fields_and_revision_bounds_are_required(self):
        payload = self.request().model_dump(mode="json")
        for patch in ({"confirmation_id": None}, {"confirmation_revision": None},
                      {"confirmation_revision": 0}, {"confirmation_revision": 2 ** 63},
                      {"confirmation_revision": True}, {"confirmation_revision": "1"}):
            with self.assertRaises(ValidationError):
                ExperimentGoalSelectionRequest.model_validate({**payload, **patch})

    def test_failure_before_commit_rolls_back_order_and_retry_is_accepted(self):
        data = self.request()
        with patch("core.routers.subscriptions._upsert_experiment_exposure",
                   side_effect=RuntimeError("injected before-commit failure")):
            with self.assertRaises(HTTPException) as caught:
                self.send(data)
        self.assertEqual(caught.exception.status_code, 500)
        self.assertIsNone(self.session.get(ExperimentGoalConfirmation, data.experiment.exposure_id))
        self.assertIsNone(self.stored())
        self.assertEqual(self.send(data)["status"], "accepted")

    def test_legacy_unknown_build_and_testflight_are_accepted_without_production_relabeling(self):
        legacy_context = self.context().model_copy(update={"app_version": "unknown", "build_number": "unknown"})
        self.send(self.request(None, context=legacy_context))
        self.assertEqual(self.stored().app_version, "unknown")
        installation = uuid4()
        testflight = self.context().model_copy(update={
            "installation_id": installation,
            "exposure_id": f"activation_experience_2026_08:{installation}",
            "analytics_environment": "testflight", "app_version": "3.8", "build_number": "119"})
        self.send(self.request(context=testflight))
        self.assertEqual(self.session.get(ExperimentGoalSelection, testflight.exposure_id).analytics_environment, "testflight")

    def test_other_account_cannot_replay_or_advance_a_known_exposure(self):
        accepted = self.request()
        self.send(accepted)
        attacker = User(id="other-account")
        self.session.add(attacker)
        self.session.commit()
        for data in (accepted, self.request(999, ["flight_history"]),
                     self.request(None, ["flight_history"], timestamp=999999)):
            with self.assertRaises(HTTPException) as caught:
                report_experiment_goal_selection(data, attacker, self.session)
            self.assertEqual(caught.exception.status_code, 403)
        ledger = self.session.get(ExperimentGoalConfirmation, accepted.experiment.exposure_id)
        self.assertEqual(ledger.confirmation_revision, 1)
        self.assertEqual(ledger.confirmation_id, str(accepted.confirmation_id))
        self.assertEqual(self.stored().selected_goal_keys, "family_friends")
        self.assertEqual(self.stored().user_id, self.user.id)

    def test_other_account_cannot_claim_goals_for_exposure_only_owner(self):
        data = self.request(999)
        goals.report_experiment_exposure(data.experiment, self.user, self.session)
        attacker = User(id="other-account")
        self.session.add(attacker)
        self.session.commit()
        with self.assertRaises(HTTPException) as caught:
            report_experiment_goal_selection(data, attacker, self.session)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIsNone(self.session.get(ExperimentGoalConfirmation, data.experiment.exposure_id))
        self.assertIsNone(self.stored())
        exposure = self.session.get(ExperimentExposure, data.experiment.exposure_id)
        self.assertEqual(exposure.user_id, self.user.id)

    def test_historical_confirmation_identity_cannot_be_reused_after_newer_acceptance(self):
        a = self.request(1)
        b = self.request(2, ["flight_history"])
        self.send(a)
        self.send(b)
        self.assertEqual(self.send(a)["status"], "stale")
        reused = a.model_copy(update={"confirmation_revision": 3, "selected_goal_keys": ["copilot_insights"]})
        with self.assertRaises(HTTPException) as caught:
            self.send(reused)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.stored().selected_goal_keys, "flight_history")
        receipts = self.session.exec(select(ExperimentGoalConfirmationReceipt)).all()
        self.assertEqual(len(receipts), 2)
        self.assertEqual({receipt.confirmation_revision for receipt in receipts}, {1, 2})


class ConcurrentGoalConfirmationTests(GoalConfirmationTests):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sofly-goal-order-")
        self.engine = create_engine(f"sqlite:///{Path(self.directory.name) / 'test.db'}",
                                   connect_args={"check_same_thread": False, "timeout": 10})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(id="concurrent-test-user")
        self.session.add(self.user)
        self.session.commit()

    def tearDown(self):
        super().tearDown()
        self.directory.cleanup()

    def parallel(self, payloads):
        self.session.rollback()
        user_id = "concurrent-test-user"
        barrier = Barrier(len(payloads))
        def send(data):
            with Session(self.engine) as independent_session:
                # Independent authenticated-session reads precede the route.
                user = independent_session.get(User, user_id)
                barrier.wait(timeout=10)
                try:
                    return report_experiment_goal_selection(data, user, independent_session)
                except HTTPException as error:
                    return error.status_code
        with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
            return list(pool.map(send, payloads))

    def test_concurrent_first_inserts_and_existing_updates_keep_maximum_revision(self):
        for start in (1, 7):
            payloads = [self.request(revision, ["flight_history"] if revision == start + 5 else ["family_friends"])
                        for revision in reversed(range(start, start + 6))]
            outcomes = self.parallel(payloads)
            self.assertTrue(all(isinstance(item, dict) for item in outcomes), outcomes)
            self.assertEqual(self.stored().selected_goal_keys, "flight_history")
            ledger = self.session.get(ExperimentGoalConfirmation, self.context().exposure_id)
            self.assertEqual(ledger.confirmation_revision, start + 5)
            self.assertEqual(len(self.session.exec(select(ExperimentGoalSelection)).all()), 1)

    def test_concurrent_equal_identical_and_equal_conflicting_first_insert(self):
        data = self.request()
        outcomes = self.parallel([data, data])
        self.assertEqual(sorted(item["status"] for item in outcomes), ["accepted", "idempotent"])
        conflicts = self.parallel([self.request(2), self.request(2, ["flight_history"])])
        self.assertEqual(sum(item == 409 for item in conflicts), 1)
        self.assertEqual(sum(isinstance(item, dict) for item in conflicts), 1)

    def test_concurrent_historical_identity_reuse_is_rejected_but_new_id_can_advance(self):
        a = self.request(1)
        self.send(a)
        self.send(self.request(2, ["flight_history"]))
        reused = a.model_copy(update={"confirmation_revision": 4})
        newest = self.request(3, ["copilot_insights"])
        outcomes = self.parallel([reused, newest])
        self.assertEqual(outcomes[0], 409)
        self.assertEqual(outcomes[1]["status"], "accepted")
        self.assertEqual(self.stored().selected_goal_keys, "copilot_insights")


class ExplicitReportParametersTests(reports.RevisedExperimentTests):
    def test_http_numeric_parameters_auth_validation_and_unchanged_aggregates(self):
        context = self.context()
        self.enroll(context)
        self.trial(context)
        app = FastAPI()
        app.include_router(router, prefix="/subscriptions")
        def db():
            with Session(self.engine) as session:
                yield session
        app.dependency_overrides[get_session] = db
        client = TestClient(app)
        path = "/subscriptions/experiments/paywall_flight_detail_2026_09/summary"
        headers = {"Authorization": f"Bearer {settings.LAMBDA_FUNCTION_AUTH_TOKEN}"}
        self.assertEqual(client.get(path).status_code, 401)
        baseline = client.get(path, headers=headers).json()
        for revision in (1, 2):
            for horizon in (14, 30):
                response = client.get(path, headers=headers, params={"measurement_revision": revision, "horizon_days": horizon})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["measurement_revision"], revision)
                self.assertEqual(response.json()["maturity_horizon_days"], horizon)
                if revision == 2:
                    self.assertEqual(response.json()["arms"][0]["verified_trial_installations"], baseline["arms"][0]["verified_trial_installations"])
        for params in ({"measurement_revision": 3}, {"horizon_days": 7}, {"horizon_days": "bad"},
                       {"reporting_window": "full_release"}):
            self.assertEqual(client.get(path, headers=headers, params=params).status_code, 422)
        params = dict(app_version="3.7", build_number="117", since_ms=self.START,
                      reporting_window="full_release", measurement_revision=2, horizon_days=14)
        full = client.get(path, headers=headers, params=params).json()
        monitor = client.get(path, headers=headers, params={**params, "reporting_window": "monitoring_window", "since_ms": self.START + 1}).json()
        self.assertEqual(full["reporting_window"], "full_release")
        self.assertEqual(full["cohort_versions"], [{"app_version": "3.7", "build_number": "117", "eligible_installations": 1}])
        self.assertEqual(monitor["reporting_window"], "monitoring_window")
        self.assertEqual(monitor["arms"], [])
        wrong_build = client.get(path, headers=headers, params={**params, "build_number": "119"}).json()
        self.assertEqual(wrong_build["arms"], [])
        lifecycle = path.replace("/summary", "/lifecycle-summary")
        self.assertEqual(client.get(lifecycle).status_code, 401)
        for revision in (1, 2):
            response = client.get(lifecycle, headers=headers, params={"measurement_revision": revision})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["measurement_revision"], revision)
        for revision in (0, 3, "bad"):
            self.assertEqual(client.get(lifecycle, headers=headers,
                                        params={"measurement_revision": revision}).status_code, 422)


class SearchRecoveryContractTests(unittest.IsolatedAsyncioTestCase):
    def test_missing_airline_typed_correction_preserves_number_date_and_query(self):
        recovery = GeminiService.preflight_recovery("4320 2026-09-10")
        self.assertEqual(recovery.flight_number, "4320")
        self.assertEqual(recovery.normalized_query, "4320")
        self.assertEqual(recovery.departure_date, "2026-09-10")
        self.assertEqual(recovery.recovery_query, "4320 2026-09-10")
        self.assertEqual([item.kind for item in recovery.suggestions], ["add_airline", "search_route"])
        self.assertEqual(GeminiService._flight_from_query("B6 4320 2026-09-10"), ("B6", "4320"))
        self.assertIsNone(GeminiService.preflight_recovery("B6 4320 2026-09-10"))
        self.assertIsNone(GeminiService.preflight_recovery("BA123"))
        self.assertIsNone(GeminiService.preflight_recovery("6E123"))

    def test_bounded_exclusions_match_unchanged_exact_and_airport_eligibility(self):
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)
        make = search.ProviderAndRankingTests._flight
        enum = search.FlightStatusEnum
        cases = [(enum.ARRIVED, "past"), (enum.CANCELED, "cancelled"),
                 (enum.CANCELEDUNCERTAIN, "cancelled"), (enum.UNKNOWN, "unknown_status"),
                 (enum.EXPECTED, None), (enum.ENROUTE, None)]
        for status, reason in cases:
            flight = make(identifier=1, number="BA123", status=status, departure_time="2026-08-17 12:00Z")
            self.assertEqual(FlightQueryHandler.search_exclusion_reason(flight, now=now), reason)
            airport = search.AirportFlightRead.model_validate(flight.model_dump())
            self.assertEqual(FlightQueryHandler.search_exclusion_reason(airport, now=now), reason)
        missing = make(identifier=1, number="BA123", status=enum.EXPECTED, departure_time="2026-08-17 12:00Z")
        missing.arrival = None
        missing.departure = None
        self.assertEqual(FlightQueryHandler.search_exclusion_reason(missing, now=now), "missing_timing")

    async def test_mixed_exclusions_accumulate_through_bounded_future_fallback(self):
        statuses = iter([search.FlightStatusEnum.CANCELED, search.FlightStatusEnum.UNKNOWN,
                         search.FlightStatusEnum.ARRIVED, search.FlightStatusEnum.CANCELED])
        calls = []
        async def handler(**kwargs):
            calls.append(kwargs["departure_date"])
            return QuerySearchResponse(flights_result=[search.ProviderAndRankingTests._flight(
                identifier=1, number="BA123", status=next(statuses), departure_time="2026-08-17 12:00Z")])
        result = await _execute_search_with_date_fallback_details(
            resolved_call=ResolvedFunctionCall(function_name="extract_flight_info",
                args={"airline_iata": "BA", "flight_number": "123", "departure_date": "2026-08-17"}, handler=handler),
            query="BA123", session=None, now=datetime(2026, 8, 17, tzinfo=timezone.utc))
        self.assertEqual(len(calls), 4)
        self.assertEqual(result.exclusion_reasons, {"cancelled", "unknown_status", "past"})
        self.assertEqual(result.provider_result_count, 4)
        self.assertEqual(result.filtered_result_count, 4)
        self.assertFalse(result.only_landed_results)
        self.assertEqual(result.response.flights_result, [])


class SearchRecoveryEndpointTests(unittest.IsolatedAsyncioTestCase):
    setUp = search_reports.SearchFailureReportingTests.setUp
    tearDown = search_reports.SearchFailureReportingTests.tearDown

    async def search_with_handler(self, handler):
        resolved = ResolvedFunctionCall(function_name="extract_flight_info",
            args={"airline_iata": "BA", "flight_number": "123", "departure_date": "2026-08-17"}, handler=handler)
        with patch("core.routers.flights.GeminiService.get_function_call", new=AsyncMock(return_value=resolved)):
            return await search_flights_from_text_post(
                payload=SearchQueryRequest(term="BA123 2026-08-17", app_version="3.8", build_number="119"),
                accept_language="en", session=self.session, user=self.user)

    async def test_filtered_endpoint_returns_edit_only_actions_without_changing_date(self):
        for status, reason, exclusion in (
            (search.FlightStatusEnum.ARRIVED, "landed_only", "past"),
            (search.FlightStatusEnum.CANCELED, "results_filtered_out", "cancelled"),
            (search.FlightStatusEnum.UNKNOWN, "results_filtered_out", "unknown_status"),
        ):
            calls = []
            async def handler(**kwargs):
                calls.append(kwargs["departure_date"])
                return QuerySearchResponse(flights_result=[search.ProviderAndRankingTests._flight(
                    identifier=1, number="BA123", status=status, departure_time="2026-08-17 12:00Z")])
            response = await self.search_with_handler(handler)
            self.assertEqual(response.recovery.reason, reason)
            self.assertEqual(response.recovery.exclusion_reason, exclusion)
            self.assertEqual(response.recovery.recovery_query, "BA123 2026-08-17")
            self.assertEqual([item.kind for item in response.recovery.suggestions], ["change_date", "search_route"])
            # Old clients ignore kind and auto-submit nonempty query values.
            self.assertTrue(all(item.query == "" for item in response.recovery.suggestions))
            self.assertEqual(response.diagnostics.provider_outcome, "results")
            self.assertEqual(calls, ["2026-08-17"])

    async def test_outage_and_rate_limit_remain_operational_not_exclusion_recovery(self):
        for rate_limited, reason in ((False, "provider_unavailable"), (True, "provider_rate_limited")):
            async def handler(**kwargs):
                raise AerodataboxUnavailableError(["status_429" if rate_limited else "status_503"])
            response = await self.search_with_handler(handler)
            self.assertEqual(response.recovery.reason, reason)
            self.assertEqual(response.diagnostics.provider_outcome, reason)
            self.assertIsNone(response.recovery.exclusion_reason)
            self.assertEqual(response.recovery.suggestions, [])


if __name__ == "__main__":
    unittest.main()
