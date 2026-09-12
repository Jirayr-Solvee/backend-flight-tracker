"""Schema-20 projection boundaries, independent of production data and SDKs."""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4
from unittest.mock import patch

from tests import test_experiment_reporting as fixtures

from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine, select

from core.activation_journey_contract import ActivationJourneyAssignmentRequest, ActivationJourneyEnrollmentRequest
from core.models.activation_journey import ActivationJourneyDiagnosticContext, ActivationJourneyEnrollment, ActivationJourneyIdentity
from core.models.experiment import ExperimentDiagnosticEvent, current_time_ms
from core.models.user import User
from core.routers.activation_journey import assignment, enrollment
from core.routers.experiment_diagnostics import DiagnosticBatch, DiagnosticEvent, get_diagnostic_report, record_diagnostic_events
from scripts.verify_analytics_wire import ensure_isolated_execution, event_fragments


class AnalyticsDiagnosticsTests(unittest.TestCase):
    setUp = fixtures.ExperimentReportingTests.setUp
    tearDown = fixtures.ExperimentReportingTests.tearDown

    def event(self, name="app_launched", *, properties=None, schema=20, installation=None, experiment=None, journey=None, environment="development", event_id=None, occurred_at=None):
        return DiagnosticEvent(
            event_id=event_id or uuid4(), event_name=name, occurred_at_ms=occurred_at if occurred_at is not None else current_time_ms(),
            installation_id=installation or uuid4(), app_version="3.8", build_number="130",
            analytics_environment=environment, build_configuration="debug" if environment == "development" else "release",
            experiment=experiment, journey=journey, properties={"event_schema_version": schema, **(properties or {})},
        )

    def send(self, *events, user=None):
        return record_diagnostic_events(DiagnosticBatch(events=list(events)), user or self.user, self.session)

    def test_cohortless_start_and_completion_do_not_reserve_or_invent_cohorts(self):
        installation = uuid4()
        start = self.event("onboarding_started", installation=installation)
        end = self.event("onboarding_completed", installation=installation)
        self.assertEqual(self.send(start, end)["accepted"], 2)
        self.assertEqual(self.send(start, end)["duplicates"], 2)
        self.assertEqual(self.session.exec(select(ActivationJourneyIdentity)).all(), [])
        self.assertEqual(self.session.exec(select(ActivationJourneyEnrollment)).all(), [])
        self.assertEqual(self.session.exec(select(ActivationJourneyDiagnosticContext)).all(), [])
        request = ActivationJourneyAssignmentRequest(
            installation_id=installation, enrollment_event_id=uuid4(), enrolled_at_ms=current_time_ms(),
            app_version="3.8", build_number="130", analytics_environment="development", is_new_installation=True,
        )
        context = assignment(request, self.user, self.session)["journey"]
        enrollment(ActivationJourneyEnrollmentRequest(journey=context), self.user, self.session)
        self.assertEqual(len(self.session.exec(select(ActivationJourneyEnrollment)).all()), 1)

    def test_pre20_unscoped_and_explicit_legacy_onboarding_keep_migration_boundary(self):
        for schema, explicit in ((19, False), (20, True)):
            with self.subTest(schema=schema, explicit=explicit):
                installation = uuid4()
                experiment = fixtures.ExperimentReportingTests.context().model_copy(update={
                    "installation_id": installation,
                    "exposure_id": f"activation_experience_2026_08:{installation}",
                    "analytics_environment": "development",
                }) if explicit else None
                self.send(self.event("onboarding_started", schema=schema, installation=installation, experiment=experiment))
                request = ActivationJourneyAssignmentRequest(
                    installation_id=installation, enrollment_event_id=uuid4(), enrolled_at_ms=current_time_ms(),
                    app_version="3.8", build_number="130", analytics_environment="development", is_new_installation=True,
                )
                with self.assertRaises(HTTPException) as error:
                    assignment(request, self.user, self.session)
                self.assertEqual(error.exception.status_code, 409)

    def test_new_event_minimum_fields_and_schema_are_required(self):
        fixtures_by_name = {
            "voice_search_action": {"action": "recording_started", "source": "search", "has_transcript": False},
            "permission_result": {"permission": "microphone", "status": "authorized", "source": "search"},
            "setting_changed": {"setting": "time_format", "value": "24_hour", "source": "profile"},
            "search_suggestion_selected": {"suggestion_kind": "example", "source": "search"},
            "account_action": {"action": "apple_sign_in", "outcome": "succeeded", "source": "profile"},
            "flight_import_action": {"action": "forwarding_address_share", "outcome": "completed", "source": "profile"},
            "flight_deletion_outcome": {"flight_id": 42, "stage": "local_save", "outcome": "succeeded"},
        }
        for name, properties in fixtures_by_name.items():
            self.assertEqual(self.send(self.event(name, properties=properties))["accepted"], 1)
            with self.assertRaises(ValidationError):
                self.event(name, properties=properties, schema=19)
            for omitted in properties:
                with self.subTest(name=name, omitted=omitted), self.assertRaises(ValidationError):
                    self.event(name, properties={key: value for key, value in properties.items() if key != omitted})

    def test_new_interaction_codes_are_finite_not_arbitrary_safe_looking_text(self):
        valid_settings = {"distance_unit": ("km", "mi"), "time_format": ("12_hour", "24_hour"),
                          "history_sort": ("date_ascending", "date_descending", "airline_ascending", "airline_descending",
                                           "departure_ascending", "departure_descending", "arrival_ascending", "arrival_descending")}
        for setting, values in valid_settings.items():
            for value in values:
                self.event("setting_changed", properties={"setting": setting, "value": value, "source": "profile"})
        for properties in ({"setting": "distance_unit", "value": "24_hour", "source": "profile"},
                           {"setting": "time_format", "value": "private_identifier", "source": "profile"}):
            with self.assertRaises(ValidationError):
                self.event("setting_changed", properties=properties)
        for permission in ("speech", "microphone", "tracking", "location"):
            for status in ("authorized", "denied", "restricted", "not_determined", "unknown", "priming_declined"):
                self.event("permission_result", properties={"permission": permission, "status": status, "source": "fixture"})
        with self.assertRaises(ValidationError):
            self.event("permission_result", properties={"permission": "microphone", "status": "private_identifier", "source": "fixture"})
        for action in ("microphone_tapped", "priming_shown", "priming_declined", "recording_started", "recording_stopped", "submitted", "failed"):
            self.event("voice_search_action", properties={"action": action, "source": "search", "has_transcript": False})
        for key in ("action", "reason"):
            with self.assertRaises(ValidationError):
                self.event("voice_search_action", properties={"action": "failed", "source": "search", "has_transcript": False, key: "private_identifier"})
        with self.assertRaises(ValidationError):
            self.event("search_suggestion_selected", properties={"suggestion_kind": "private_identifier", "source": "search"})
        for action in ("guest_create", "apple_sign_in", "sign_out", "account_delete"):
            for outcome in ("started", "succeeded", "cancelled", "failed"):
                self.event("account_action", properties={"action": action, "outcome": outcome, "source": "profile"})
        for outcome in ("presented", "completed", "cancelled", "failed"):
            self.event("flight_import_action", properties={"action": "forwarding_address_share", "outcome": outcome, "source": "profile"})
        for name, properties in (("account_action", {"action": "private_identifier", "outcome": "succeeded", "source": "profile"}),
                                 ("flight_import_action", {"action": "forwarding_address_share", "outcome": "flight_imported", "source": "profile"})):
            with self.assertRaises(ValidationError):
                self.event(name, properties=properties)

    def test_extended_tokens_reject_human_text_contacts_urls_and_oversize_values(self):
        keys = ("screen", "app_language", "permission", "setting", "value", "choice_key", "update_type", "level", "confidence",
                "exposure_scope", "enrollment_scope", "completion_semantics", "selection_stage", "view_scope", "load_scope", "layout")
        for key in keys:
            for value in ("synthetic@example.invalid", "https://example.invalid/path", "+1 555 000 0000", "private route text", "line\nbreak", "x" * 81):
                with self.subTest(key=key), self.assertRaises(ValidationError):
                    self.event(properties={key: value})
        # Syntactic token checks do not magically recognize all PII: capture
        # must still select fixed machine codes, never arbitrary user strings.

    def test_deletion_intent_is_distinct_from_finite_persistence_outcomes(self):
        for stage, outcomes in (("local_save", ("succeeded", "failed")),
                                ("backend_delete", ("succeeded", "failed", "skipped"))):
            for outcome in outcomes:
                event = self.event("flight_deletion_outcome", properties={"flight_id": 42, "stage": stage, "outcome": outcome})
                self.assertEqual(self.send(event)["accepted"], 1)
                self.assertEqual(self.send(event)["duplicates"], 1)
        for stage, outcome in (("local_save", "skipped"), ("requested", "succeeded"),
                               ("backend_delete", "cancelled"), ("private_identifier", "succeeded")):
            with self.subTest(stage=stage, outcome=outcome), self.assertRaises(ValidationError):
                self.event("flight_deletion_outcome", properties={"flight_id": 42, "stage": stage, "outcome": outcome})
        for invalid_id in (0, -1, True, "42"):
            with self.subTest(flight_id=invalid_id), self.assertRaises(ValidationError):
                self.event("flight_deletion_outcome", properties={"flight_id": invalid_id, "stage": "local_save", "outcome": "succeeded"})
        # Historical intent rows remain readable; schema 20 marks intent
        # explicitly without relabeling past requests as successful deletes.
        self.event("flight_deleted", schema=19, properties={"flight_id": 42})
        self.event("flight_deleted", properties={"flight_id": 42, "stage": "requested"})

    def test_raw_sensitive_properties_remain_forbidden_after_expansion(self):
        for key in ("query", "normalized_query", "transcript", "route", "callsign", "icao24", "notification_body", "notification_id", "jws_payload", "email", "detail", "choice_name"):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.event(properties={key: "synthetic-private-value"})

    def test_boolean_and_integer_fields_are_strict_with_exact_bounds(self):
        for key in ("has_transcript", "has_active_entitlement", "has_flight_context", "has_live_telemetry", "shows_summary_action", "selected"):
            for value in (True, False):
                self.event(properties={key: value})
            for value in (0, 1, "true", "false"):
                with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                    self.event(properties={key: value})
        bounds = {"selected_count": (0, 20), "score": (0, 100), "measurement_revision": (1, 100),
                  "source_flight_id": (1, 9_223_372_036_854_775_807), "new_flight_id": (1, 9_223_372_036_854_775_807),
                  "trial_duration_days": (0, 365), "attempt_count": (0, 1000)}
        for key, (minimum, maximum) in bounds.items():
            self.event(properties={key: minimum})
            self.event(properties={key: maximum})
            for value in (minimum - 1, maximum + 1, True, "1", 1.25):
                with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                    self.event(properties={key: value})

    def test_cohortless_capture_preserves_environment_owner_and_original_facts(self):
        installation = uuid4()
        event = self.event("app_launched", installation=installation)
        self.send(event)
        self.assertEqual(get_diagnostic_report(limit=100, session=self.session)["count"], 0)
        self.assertEqual(get_diagnostic_report(analytics_environment="development", limit=100, session=self.session)["count"], 1)
        changed = event.model_copy(update={"analytics_environment": "testflight", "build_configuration": "release"})
        with self.assertRaises(HTTPException) as conflict:
            self.send(changed)
        self.assertEqual(conflict.exception.status_code, 409)
        other = User(id="other-synthetic-user")
        self.session.add(other)
        self.session.commit()
        with self.assertRaises(HTTPException) as conflict:
            self.send(event, user=other)
        self.assertEqual(conflict.exception.status_code, 409)
        stored = self.session.get(ExperimentDiagnosticEvent, str(event.event_id))
        self.assertEqual(stored.user_id, self.user.id)
        self.assertEqual(stored.analytics_environment, "development")
        self.assertEqual(json.loads(stored.properties_json), {"event_schema_version": 20})

    def test_retention_deletes_context_before_event_with_foreign_keys_enabled(self):
        self.session.close()
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
        self.session = Session(self.engine)
        self.user = self.session.get(User, "test-user")
        past = current_time_ms() - 91 * 86_400_000
        request = ActivationJourneyAssignmentRequest(
            installation_id=uuid4(), enrollment_event_id=uuid4(), enrolled_at_ms=past,
            app_version="3.8", build_number="130", analytics_environment="development", is_new_installation=True,
        )
        with patch("core.services.activation_journey.current_time_ms", return_value=past + 1000):
            context = assignment(request, self.user, self.session)["journey"]
            enrollment(ActivationJourneyEnrollmentRequest(journey=context), self.user, self.session)
        expired = self.event("onboarding_started", installation=context.installation_id, journey=context, occurred_at=past + 2000)
        with patch("core.routers.experiment_diagnostics.current_time_ms", return_value=past + 3000):
            self.send(expired)
        row = self.session.get(ExperimentDiagnosticEvent, str(expired.event_id))
        row.received_at_ms = past + 3000
        self.session.add(row)
        self.session.commit()
        self.assertIsNotNone(self.session.get(ActivationJourneyDiagnosticContext, row.id))
        live = self.event("app_launched", installation=context.installation_id, journey=context)
        self.send(live)
        self.assertIsNone(self.session.get(ActivationJourneyDiagnosticContext, str(expired.event_id)))
        self.assertIsNone(self.session.get(ExperimentDiagnosticEvent, str(expired.event_id)))
        self.assertIsNotNone(self.session.get(ActivationJourneyDiagnosticContext, str(live.event_id)))
        identity = context.exposure_id + ":v1"
        self.assertIsNotNone(self.session.get(ActivationJourneyEnrollment, identity))
        self.assertIsNotNone(self.session.get(ActivationJourneyIdentity, identity))


class AnalyticsMigrationRaceTests(unittest.TestCase):
    def test_independent_workers_serialize_old_boundary_but_allow_cohortless_capture(self):
        for schema in (19, 20):
            with self.subTest(schema=schema), tempfile.TemporaryDirectory(prefix="sofly-cohortless-race-") as scratch:
                engine = create_engine(f"sqlite:///{Path(scratch) / 'fixture.db'}", connect_args={"check_same_thread": False, "timeout": 20})
                try:
                    SQLModel.metadata.create_all(engine)
                    with Session(engine) as session:
                        session.add(User(id="race-owner"))
                        session.commit()
                    installation = uuid4()
                    request = ActivationJourneyAssignmentRequest(
                        installation_id=installation, enrollment_event_id=uuid4(), enrolled_at_ms=current_time_ms(),
                        app_version="3.8", build_number="130", analytics_environment="development", is_new_installation=True,
                    )
                    event = DiagnosticEvent(
                        event_id=uuid4(), event_name="onboarding_started", occurred_at_ms=current_time_ms(),
                        installation_id=installation, app_version="3.8", build_number="130", analytics_environment="development",
                        build_configuration="debug", properties={"event_schema_version": schema},
                    )
                    barrier = Barrier(2)
                    def writer(operation):
                        with Session(engine) as session:
                            user = session.get(User, "race-owner")
                            barrier.wait(timeout=10)
                            try:
                                if operation == "assignment":
                                    assignment(request, user, session)
                                else:
                                    record_diagnostic_events(DiagnosticBatch(events=[event]), user, session)
                                return 200
                            except HTTPException as error:
                                session.rollback()
                                return error.status_code
                    with ThreadPoolExecutor(max_workers=2) as workers:
                        results = list(workers.map(writer, ("assignment", "diagnostic")))
                    self.assertEqual(sorted(results), [200, 200] if schema == 20 else [200, 409])
                    with Session(engine) as session:
                        reservations = session.exec(select(ActivationJourneyIdentity)).all()
                        self.assertEqual(len(reservations), 1)
                        if schema == 20:
                            self.assertEqual(reservations[0].protocol, "journey")
                            self.assertEqual(len(session.exec(select(ExperimentDiagnosticEvent)).all()), 1)
                finally:
                    engine.dispose()


class AnalyticsWireSafetyTests(unittest.TestCase):
    def test_inherited_app_credentials_are_rejected_before_any_app_import(self):
        repository = Path(__file__).resolve().parents[1]
        with patch.dict(os.environ, {"PYTHONPATH": str(repository), "JWT_SECRET": "synthetic-private-marker"}, clear=True):
            with self.assertRaises(SystemExit) as error:
                ensure_isolated_execution(repository)
            self.assertNotIn("synthetic-private-marker", str(error.exception))

    def test_wire_fragment_extraction_keeps_original_object_bytes(self):
        raw = b'[\n  { "event_name" : "one", "number": 1 },\n {"event_name":"two","value":"brace } inside"}\n]'
        self.assertEqual(list(event_fragments(raw)), [b'{ "event_name" : "one", "number": 1 }',
                                                   b'{"event_name":"two","value":"brace } inside"}'])


if __name__ == "__main__":
    unittest.main()
