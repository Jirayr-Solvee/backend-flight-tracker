"""Synthetic-only negative tests for auth, token and cleanup log boundaries."""

import asyncio
import logging
import traceback
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Install the repository's existing inert test configuration before core imports.
from tests import test_experiment_reporting as _test_environment

from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pydantic_settings import BaseSettings
from sqlalchemy.exc import StatementError
from sqlmodel import Session

from core import models
from core.config import Settings
from core.dependency import get_current_user
from core.models.user import User
from core.routers import users


APPLE_TOKEN = "FAKE_APPLE_CREDENTIAL_DO_NOT_LOG"
SESSION_TOKEN = "FAKE_SESSION_CREDENTIAL_DO_NOT_LOG"
PUSH_TOKEN = "ab" * 32
EMAIL = "synthetic-private@example.invalid"
NAME = "Synthetic Private Name"
SUBJECT = "FAKE_PRIVATE_SUBJECT"
SENTINELS = (APPLE_TOKEN, SESSION_TOKEN, PUSH_TOKEN, EMAIL, NAME, SUBJECT)


def unsafe_database_error():
    return StatementError(
        "injected driver failure " + APPLE_TOKEN,
        "UPDATE credential_fixture SET value = ?",
        {"jwt": SESSION_TOKEN, "push_token": PUSH_TOKEN, "email": EMAIL},
        RuntimeError("injected chained failure " + NAME),
    )


class CredentialLoggingTests(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock(spec=Session)
        self.user = User(id="synthetic-guest")
        self.request = users.CreateUserRequest(
            apple_jwt=APPLE_TOKEN, full_name=NAME, email=EMAIL,
        )

    def assert_private_logs(self, capture):
        self.assertTrue(capture.records)
        for record in capture.records:
            rendered = logging.Formatter("%(levelname)s %(message)s").format(record)
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, rendered)
                self.assertNotIn(sentinel, repr(record.__dict__))
            self.assertFalse(record.exc_info)
            self.assertIsNone(record.stack_info)

    def assert_private_exception(self, error, status=500):
        self.assertEqual(error.status_code, status)
        self.assertTrue(error.__suppress_context__)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, "".join(traceback.format_exception(error)))

    def sign_in(self):
        return asyncio.run(users.create_user(self.request, self.user, self.session))

    def test_sign_in_verifier_failure_never_logs_request_exception_or_claims(self):
        with patch.object(users, "verify_apple_identity_token", new=AsyncMock(
            side_effect=RuntimeError(" ".join(SENTINELS)),
        )), self.assertLogs(level="ERROR") as logs, self.assertRaises(HTTPException) as error:
            self.sign_in()
        self.assert_private_logs(logs)
        self.assert_private_exception(error.exception)
        self.session.rollback.assert_called_once()

    def test_sign_in_sql_parameters_and_chained_error_are_not_logged(self):
        self.session.exec.side_effect = unsafe_database_error()
        with patch.object(users, "verify_apple_identity_token", new=AsyncMock(
            return_value={"sub": SUBJECT, "email": EMAIL},
        )), self.assertLogs(level="ERROR") as logs, self.assertRaises(HTTPException) as error:
            self.sign_in()
        self.assert_private_logs(logs)
        self.assert_private_exception(error.exception)

    def test_commit_failure_and_failing_rollback_are_both_bounded(self):
        self.session.exec.return_value.first.return_value = None
        self.session.commit.side_effect = unsafe_database_error()
        self.session.rollback.side_effect = RuntimeError(SESSION_TOKEN)
        with patch.object(users, "verify_apple_identity_token", new=AsyncMock(
            return_value={"sub": SUBJECT},
        )), patch.object(users, "create_jwt", return_value=SESSION_TOKEN), \
                patch.object(users, "record_verified_apple_email_identity"), \
                self.assertLogs(level="ERROR") as logs, self.assertRaises(HTTPException) as error:
            self.sign_in()
        self.assert_private_logs(logs)
        self.assert_private_exception(error.exception)
        self.assertIn("rollback_failed=True", logs.output[0])

    def test_guest_credential_generation_failure_is_bounded(self):
        self.session.rollback.side_effect = unsafe_database_error()
        with patch.object(users, "create_jwt", side_effect=RuntimeError(SESSION_TOKEN)), \
                self.assertLogs(level="ERROR") as logs, self.assertRaises(HTTPException) as error:
            users.create_guest_user(self.session)
        self.assert_private_logs(logs)
        self.assert_private_exception(error.exception)

    def test_successful_sign_in_keeps_intended_response_and_commit(self):
        self.session.exec.return_value.first.return_value = None
        with patch.object(users, "verify_apple_identity_token", new=AsyncMock(
            return_value={"sub": SUBJECT},
        )), patch.object(users, "create_jwt", return_value=SESSION_TOKEN), \
                patch.object(users, "record_verified_apple_email_identity"):
            response = self.sign_in()
        self.assertEqual(response.jwt, SESSION_TOKEN)
        self.assertEqual(response.email, EMAIL)
        self.assertEqual(response.full_name, NAME)
        self.assertTrue(self.user.verified)
        self.session.commit.assert_called_once()
        self.session.rollback.assert_not_called()

    def test_existing_account_sign_in_does_not_rewrite_account(self):
        existing = User(id="existing", verified=True, email=EMAIL, full_name=NAME)
        self.session.exec.return_value.first.return_value = existing
        with patch.object(users, "verify_apple_identity_token", new=AsyncMock(
            return_value={"sub": SUBJECT},
        )), patch.object(users, "create_jwt", return_value=SESSION_TOKEN), \
                patch.object(users, "record_verified_apple_email_identity") as proof:
            response = self.sign_in()
        self.assertEqual(response.user_id, "existing")
        self.assertEqual(response.jwt, SESSION_TOKEN)
        proof.assert_called_once()
        self.assertEqual(proof.call_args.args[1], "existing")
        self.assertEqual(existing.email, EMAIL)
        self.assertEqual(existing.full_name, NAME)
        self.session.commit.assert_called_once()

    def test_credential_model_repr_hides_fields_but_wire_json_is_unchanged(self):
        examples = [
            self.request,
            users.CreateUserResponse(jwt=SESSION_TOKEN, user_id="id", email=EMAIL, full_name=NAME),
            users.CreateGuesUserResponse(jwt=SESSION_TOKEN, guest_id="id", device_id="id"),
            users.RefreshApnToken(device_id="id", apn_token=PUSH_TOKEN),
            users.RegisterLiveActivityRequest(device_id="id", flight_id=1, push_token=PUSH_TOKEN),
            users.RegisterLiveActivityPushToStartRequest(device_id="id", push_token=PUSH_TOKEN),
        ]
        for model in examples:
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, repr(model))
                self.assertNotIn(sentinel, str(model))
        self.assertEqual(self.request.model_dump()["apple_jwt"], APPLE_TOKEN)
        self.assertEqual(examples[1].model_dump()["jwt"], SESSION_TOKEN)

    def test_auth_rejection_does_not_dump_decoded_claims_or_subject(self):
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=SESSION_TOKEN)
        for claims in ({"email": EMAIL, "name": NAME},
                       {"sub": SUBJECT, "email": EMAIL, "jwt": APPLE_TOKEN}):
            with self.subTest(has_subject="sub" in claims):
                self.session.get.return_value = None
                with patch("core.dependency.decode_jwt", return_value=claims), \
                        self.assertLogs(level="WARNING") as logs, self.assertRaises(HTTPException) as error:
                    get_current_user(self.session, credentials)
                self.assert_private_logs(logs)
                self.assert_private_exception(error.exception, 401)

    def test_auth_decode_and_rollback_failures_cannot_escape(self):
        self.session.rollback.side_effect = unsafe_database_error()
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=SESSION_TOKEN)
        with patch("core.dependency.decode_jwt", side_effect=RuntimeError(SESSION_TOKEN)), \
                self.assertLogs(level="ERROR") as logs, self.assertRaises(HTTPException) as error:
            get_current_user(self.session, credentials)
        self.assert_private_logs(logs)
        self.assert_private_exception(error.exception, 401)

    def test_apn_database_failure_does_not_log_push_token(self):
        self.session.exec.side_effect = unsafe_database_error()
        with self.assertLogs(level="ERROR") as logs, self.assertRaises(HTTPException) as error:
            users.refresh_apn_token(
                users.RefreshApnToken(device_id="id", apn_token=PUSH_TOKEN),
                self.user, self.session,
            )
        self.assert_private_logs(logs)
        self.assert_private_exception(error.exception)

    def test_session_close_and_invalidate_failures_do_not_escape_or_log_values(self):
        self.session.close.side_effect = unsafe_database_error()
        self.session.invalidate.side_effect = RuntimeError(SESSION_TOKEN)
        with patch.object(models, "Session", return_value=self.session):
            generator = models.get_session()
            self.assertIs(next(generator), self.session)
            with self.assertLogs(level="ERROR") as logs:
                with self.assertRaises(StopIteration):
                    next(generator)
        self.assert_private_logs(logs)
        self.session.invalidate.assert_called_once()

    def test_normal_session_close_and_body_error_semantics_are_preserved(self):
        with patch.object(models, "Session", return_value=self.session):
            generator = models.get_session()
            next(generator)
            with self.assertRaisesRegex(ValueError, "expected-body-error"):
                generator.throw(ValueError("expected-body-error"))
        self.session.close.assert_called_once()
        self.session.invalidate.assert_not_called()
        self.assertTrue(models.engine.hide_parameters)

    def test_startup_settings_errors_hide_input_values(self):
        class StartupProbe(BaseSettings):
            model_config = Settings.model_config
            secret: str
            count: int
        self.assertTrue(Settings.model_config["hide_input_in_errors"])
        with self.assertRaises(ValidationError) as error:
            StartupProbe(_env_file=None, secret=SESSION_TOKEN, count=APPLE_TOKEN)
        self.assertNotIn(SESSION_TOKEN, str(error.exception))
        self.assertNotIn(APPLE_TOKEN, str(error.exception))


class CredentialHTTPBoundaryTests(unittest.TestCase):
    assert_private_logs = CredentialLoggingTests.assert_private_logs

    def setUp(self):
        CredentialLoggingTests.setUp(self)
        app = FastAPI()
        app.include_router(users.router, prefix="/users")
        app.dependency_overrides[models.get_session] = lambda: self.session
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()

    def test_pre_handler_database_failures_are_generic_without_server_tracebacks(self):
        for method, path, payload in (
            ("PUT", "/users/me/live-activity-push-to-start", {"device_id": "id", "push_token": PUSH_TOKEN}),
            ("PUT", "/users/me/live-activities/activity", {"device_id": "id", "flight_id": 1, "push_token": PUSH_TOKEN}),
            ("DELETE", "/users/me/live-activities/activity?device_id=id", None),
        ):
            with self.subTest(path=path):
                self.session.exec.side_effect = unsafe_database_error()
                self.session.get.side_effect = unsafe_database_error()
                with self.assertLogs(level="ERROR") as logs:
                    response = self.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json(), {"detail": "Internal server error"})
                self.assert_private_logs(logs)

    def test_expected_404_and_422_remain_unchanged(self):
        self.session.exec.return_value.first.return_value = None
        response = self.client.put("/users/me/live-activity-push-to-start", json={
            "device_id": "id", "push_token": PUSH_TOKEN,
        })
        self.assertEqual(response.status_code, 404)
        response = self.client.put("/users/me/live-activity-push-to-start", json={
            "device_id": "id", "push_token": "too-short",
        })
        self.assertEqual(response.status_code, 422)

    def test_sign_in_http_failure_has_no_credential_echo(self):
        with patch.object(users, "verify_apple_identity_token", new=AsyncMock(
            side_effect=RuntimeError(APPLE_TOKEN),
        )), self.assertLogs(level="ERROR") as logs:
            response = self.client.post("/users/me/", json=self.request.model_dump())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Internal server error"})
        self.assert_private_logs(logs)

    def test_real_auth_dependency_still_rejects_unknown_subject(self):
        self.client.app.dependency_overrides.pop(get_current_user)
        self.session.get.return_value = None
        with patch("core.dependency.decode_jwt", return_value={"sub": SUBJECT}), \
                self.assertLogs(level="WARNING") as logs:
            response = self.client.get(
                "/users/me/flights", headers={"Authorization": "Bearer " + SESSION_TOKEN},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Authorization required"})
        self.assert_private_logs(logs)


if __name__ == "__main__":
    unittest.main()
