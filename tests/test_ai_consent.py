"""Synthetic-only account/consent wire and real-egress-boundary regression tests.

Run in a disposable working directory so importing legacy core.models never
touches a developer/app database. All provider/Apple/S3 requests are stubbed.
"""

import asyncio
import io
import logging
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from tests import test_experiment_reporting as _test_environment

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select, update

from core import background_tasks
from core.config import settings
from core.models import get_session
from core.models.ai_consent import AIConsentReceipt, UserAIConsent, UserAIEmailIdentity
from core.models.email import S3EmailNotification
from core.models.flight import QuerySearchResponse
from core.models.search_failure import SearchFailureSample
from core.models.user import User
from core.routers import ai_consent, flights, users
from core.services.ai_consent import (
    AIConsentAccountUnavailable, AIConsentConflict, AIConsentRequired,
    AIConsentUnavailable, AIConsentUpdate, read_ai_consent,
    record_verified_apple_email_identity, require_ai_consent, update_ai_consent,
)
from core.services.gemini.config import email_config, query_config
from core.services.gemini.service import GeminiService
from core.services.email_ingress import require_email_consent_for_send
from core.utils import create_jwt
from scripts.migrate_ai_consent import migrate
from tests.email_ingress_fixtures import BUCKET, notification, s3_object


OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
PRIVATE = "PRIVATE_SYNTHETIC_ITINERARY_DO_NOT_LOG"
EMAIL = "synthetic-owner@example.invalid"
SECRET = "PRIVATE_SYNTHETIC_APPLE_JWT_DO_NOT_LOG"


class AIConsentTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="sofly-ai-consent-")
        self.database = Path(self.scratch.name) / "fixture.db"
        self.engine = create_engine(
            f"sqlite:///{self.database}", connect_args={"check_same_thread": False, "timeout": 10},
            hide_parameters=True,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(User(id=OWNER, apple_id="apple-owner", verified=True, email=EMAIL))
        self.session.add(User(id=OTHER, apple_id="apple-other", verified=True, email="other@example.invalid"))
        self.session.commit()
        self.owner = self.session.get(User, OWNER)
        self.models_engine = patch("core.models.engine", self.engine)
        self.models_engine.start()
        self.background_engine = patch.object(background_tasks, "engine", self.engine)
        self.background_engine.start()
        self.sdk = MagicMock()
        self.sdk.models.generate_content.return_value = None
        self.sdk_patch = patch("core.services.gemini.service.genai.Client", return_value=self.sdk)
        self.sdk_patch.start()
        app = FastAPI()
        app.include_router(ai_consent.router, prefix="/users")
        app.include_router(flights.router, prefix="/flights")
        app.include_router(users.router, prefix="/users")

        def session_dependency():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_session] = session_dependency
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.sdk_patch.stop()
        self.background_engine.stop()
        self.models_engine.stop()
        self.session.close()
        self.engine.dispose()
        self.scratch.cleanup()

    def headers(self, owner=OWNER):
        return {"Authorization": "Bearer " + create_jwt(sub=owner)}

    def snapshot(self, owner=OWNER):
        with Session(self.engine) as session:
            return read_ai_consent(session, owner)

    def command(self, *, owner=OWNER, purpose="search", enabled=True, revision=None, request_id=None):
        return AIConsentUpdate(
            user_id=owner, policy_version=1,
            expected_revision=self.snapshot(owner).revision if revision is None else revision,
            purpose=purpose, enabled=enabled, request_id=request_id or uuid4(),
        )

    def change(self, **kwargs):
        with Session(self.engine) as session:
            result = update_ai_consent(session, self.command(**kwargs))
            # Existing consent tests concern scope/revocation, not intake-time
            # ordering. Place their synthetic initial grant before mail arrival;
            # dedicated ingress tests exercise exact timestamps and pre-grant mail.
            if kwargs.get("purpose") == "forwarded_email" and kwargs.get("enabled", True):
                session.exec(update(UserAIConsent).where(UserAIConsent.user_id == result.user_id).values(
                    forwarded_email_granted_at_ms=1_700_000_000_000,
                ))
                session.commit()
            return result

    def prove_email(self, owner=OWNER, email=EMAIL):
        with Session(self.engine) as session:
            user = session.get(User, owner)
            record_verified_apple_email_identity(session, owner, {
                "sub": user.apple_id, "email": email, "email_verified": True,
            })
            session.commit()

    def test_legacy_and_new_accounts_have_default_deny_without_backfill(self):
        response = self.client.get("/users/me/ai-consent", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "user_id": OWNER, "policy_version": 1, "revision": 0,
            "search_enabled": False, "forwarded_email_enabled": False,
            "forwarded_email_verified": False, "updated_at": None,
        })
        self.assertEqual(self.session.exec(select(UserAIConsent)).all(), [])
        self.assertEqual(self.session.exec(select(UserAIEmailIdentity)).all(), [])
        self.session.add(User(id="guest"))
        self.session.commit()
        self.assertFalse(self.snapshot("guest").search_enabled)

    def test_auth_required_for_all_consent_routes_and_search(self):
        for method, path, body in (
            ("get", "/users/me/ai-consent", None),
            ("put", "/users/me/ai-consent", self.command().model_dump(mode="json")),
            ("post", "/users/me/ai-consent/verify-email", {"user_id": OWNER, "apple_jwt": SECRET}),
            ("post", "/flights/search/term", {"term": PRIVATE}),
        ):
            with self.subTest(path=path, method=method):
                kwargs = {"json": body} if body is not None else {}
                self.assertIn(getattr(self.client, method)(path, **kwargs).status_code, (401, 403))
        self.sdk.models.generate_content.assert_not_called()

    def test_frozen_owner_mismatch_does_not_mutate_other_account(self):
        response = self.client.put("/users/me/ai-consent", headers=self.headers(),
                                   json=self.command(owner=OTHER).model_dump(mode="json"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "ai_consent_account_mismatch")
        self.assertFalse(self.snapshot(OWNER).search_enabled)
        self.assertFalse(self.snapshot(OTHER).search_enabled)

    def test_wire_response_timestamp_is_integer_milliseconds_and_scopes_are_separate(self):
        response = self.client.put("/users/me/ai-consent", headers=self.headers(),
                                   json=self.command().model_dump(mode="json"))
        self.assertEqual(response.status_code, 200)
        self.assertIs(type(response.json()["updated_at"]), int)
        self.assertGreater(response.json()["updated_at"], 1_700_000_000_000)
        self.assertTrue(response.json()["search_enabled"])
        self.assertFalse(response.json()["forwarded_email_enabled"])
        require_ai_consent(OWNER, "search")
        with self.assertRaises(AIConsentRequired):
            require_ai_consent(OWNER, "forwarded_email")
        with self.assertRaises(AIConsentRequired):
            require_ai_consent(OTHER, "search")

    def test_email_allow_requires_separate_apple_email_proof_but_revoke_never_does(self):
        response = self.client.put("/users/me/ai-consent", headers=self.headers(),
                                   json=self.command(purpose="forwarded_email").model_dump(mode="json"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "ai_consent_email_verification_required")
        response = self.client.put("/users/me/ai-consent", headers=self.headers(), json=self.command(
            purpose="forwarded_email", enabled=False,
        ).model_dump(mode="json"))
        self.assertEqual(response.status_code, 200)
        self.prove_email()
        self.change(purpose="forwarded_email")
        require_ai_consent(OWNER, "forwarded_email")
        with self.assertRaises(AIConsentRequired):
            require_ai_consent(OWNER, "search")

    def test_receipt_replay_after_revoke_returns_current_denial_never_old_allow(self):
        allow = self.command()
        update_ai_consent(self.session, allow)
        revoked = self.change(enabled=False)
        replay = update_ai_consent(self.session, allow)
        self.assertEqual(replay, revoked)
        self.assertFalse(replay.search_enabled)
        self.assertEqual(replay.revision, 2)
        self.assertEqual(len(self.session.exec(select(AIConsentReceipt)).all()), 2)

    def test_stale_allow_and_mutated_request_id_are_rejected(self):
        allow = self.command()
        update_ai_consent(self.session, allow)
        self.change(enabled=False)
        for command in (self.command(revision=0), allow.model_copy(update={"enabled": False})):
            with self.subTest(command=command):
                response = self.client.put("/users/me/ai-consent", headers=self.headers(),
                                           json=command.model_dump(mode="json"))
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"]["code"], "ai_consent_conflict")
                self.assertFalse(response.json()["detail"]["consent"]["search_enabled"])

    def test_concurrent_initial_updates_accept_exactly_one_revision(self):
        barrier = threading.Barrier(2)
        commands = [self.command(revision=0, enabled=value) for value in (True, False)]

        def attempt(command):
            with Session(self.engine) as session:
                barrier.wait(timeout=5)
                try:
                    return update_ai_consent(session, command).revision
                except AIConsentConflict:
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, commands))
        self.assertCountEqual(outcomes, [1, "conflict"])
        self.assertEqual(self.snapshot().revision, 1)
        self.assertEqual(len(self.session.exec(select(AIConsentReceipt)).all()), 1)

    def test_concurrent_identical_receipt_is_idempotent(self):
        barrier = threading.Barrier(2)
        command = self.command()

        def attempt(_):
            with Session(self.engine) as session:
                barrier.wait(timeout=5)
                return update_ai_consent(session, command).revision

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, range(2)))
        self.assertEqual(outcomes, [1, 1])
        self.assertEqual(len(self.session.exec(select(AIConsentReceipt)).all()), 1)

    def test_strict_wire_validation_rejects_coercions_unknown_scope_and_missing_owner(self):
        for field, value in (("enabled", "true"), ("expected_revision", True),
                             ("purpose", "email_import"), ("policy_version", 99),
                             ("request_id", "bad"), ("user_id", "")):
            body = self.command().model_dump(mode="json")
            body[field] = value
            self.assertEqual(self.client.put("/users/me/ai-consent", headers=self.headers(), json=body).status_code, 422)
        self.assertEqual(self.snapshot().revision, 0)

    def test_revocation_is_read_fresh_despite_existing_session_and_service(self):
        self.change()
        cached = self.session.get(UserAIConsent, OWNER)
        self.assertTrue(cached.search_enabled)
        service = GeminiService(user_id=OWNER)
        self.change(enabled=False)
        with self.assertRaises(AIConsentRequired):
            require_ai_consent(OWNER, "search")
        self.sdk.models.generate_content.assert_not_called()

    def test_actual_sdk_boundary_denies_missing_account_email_and_unknown_scope(self):
        self.change()
        for owner, purpose in ((None, "search"), (OTHER, "forwarded_email"), (OWNER, "forwarded_email"), (OWNER, "unknown")):
            with self.subTest(owner=owner, purpose=purpose), self.assertRaises(AIConsentRequired):
                asyncio.run(GeminiService(user_id=owner)._generate(PRIVATE, email_config, purpose=purpose))
        self.sdk.models.generate_content.assert_not_called()

    def test_prompt_free_search_does_not_create_or_rewrite_historical_consent(self):
        for state in ("missing", "allowed", "revoked", "stale"):
            if state == "allowed":
                self.change()
            elif state == "revoked":
                self.change(enabled=False)
            elif state == "stale":
                self.session.exec(update(UserAIConsent).where(UserAIConsent.user_id == OWNER).values(policy_version=0))
                self.session.commit()
            before = self.snapshot()
            receipts = len(self.session.exec(select(AIConsentReceipt)).all())
            self.sdk.models.generate_content.reset_mock()
            asyncio.run(GeminiService(user_id=OWNER)._generate("flights from Yerevan to Mexico", query_config, purpose="search"))
            self.sdk.models.generate_content.assert_called_once()
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(len(self.session.exec(select(AIConsentReceipt)).all()), receipts)

    def test_historical_search_policy_still_denies_in_consent_api(self):
        self.change()
        self.session.exec(update(UserAIConsent).where(UserAIConsent.user_id == OWNER).values(policy_version=0))
        self.session.commit()
        with self.assertRaises(AIConsentRequired):
            require_ai_consent(OWNER, "search")
        self.sdk.models.generate_content.assert_not_called()

    def test_search_provider_failures_remain_bounded_and_private_without_consent_lookup(self):
        self.change()

        def first_send(**kwargs):
            self.change(enabled=False)
            raise RuntimeError(PRIVATE)

        self.sdk.models.generate_content.side_effect = first_send
        service = GeminiService(user_id=OWNER)
        with patch.object(service, "_deterministic_function_call", return_value=None), \
                self.assertLogs("core.services.gemini.service", level="ERROR") as captured:
            self.assertIsNone(asyncio.run(service.get_function_call(PRIVATE)))
        self.assertEqual(self.sdk.models.generate_content.call_count, 3)
        self.assertNotIn(PRIVATE, " ".join(captured.output))
        self.assertTrue(all(not record.exc_info for record in captured.records))

    def test_search_does_not_depend_on_consent_storage(self):
        service = GeminiService(user_id=OWNER)
        with patch("core.services.ai_consent.Session", side_effect=RuntimeError(PRIVATE)):
            asyncio.run(service._generate(PRIVATE, query_config, purpose="search"))
        self.sdk.models.generate_content.assert_called_once()

    def test_modern_post_and_legacy_get_ai_search_need_no_consent(self):
        self.sdk.models.generate_content.return_value = SimpleNamespace(candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(function_call=SimpleNamespace(
                name="extract_flight_info", args={"flight_number": "178", "airline_iata": "BA", "departure_date": "2026-09-30"},
            ))]),
        )])
        with patch.object(GeminiService, "_deterministic_function_call", return_value=None), \
                patch.object(GeminiService, "preflight_recovery", return_value=None), \
                patch.object(flights, "_execute_search_with_date_fallback_details", new=AsyncMock(return_value=flights.SearchExecutionResult(
                    response=QuerySearchResponse(), provider_result_count=0, filtered_result_count=0,
                    only_landed_results=False,
                ))):
            response = self.client.post("/flights/search/term", headers=self.headers(), json={"term": PRIVATE})
            self.assertEqual(response.status_code, 200)
            self.assertNotEqual(response.json().get("recovery", {}).get("reason"), "ai_consent_required")
            response = self.client.get("/flights/search/term", headers=self.headers(), params={"term": PRIVATE})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json().get("recovery", {}).get("reason"), "ai_consent_required")
        self.assertEqual(self.sdk.models.generate_content.call_count, 2)
        self.assertEqual(self.snapshot().revision, 0)
        self.assertEqual(self.session.exec(select(AIConsentReceipt)).all(), [])

    def test_old_clients_deterministic_flight_airport_and_route_need_no_consent(self):
        for term in ("BA123", "JFK", "JFK to LHR today"):
            with self.subTest(term=term), patch.object(
                flights, "_execute_search_with_date_fallback_details", new=AsyncMock(return_value=flights.SearchExecutionResult(
                    response=QuerySearchResponse(), provider_result_count=0, filtered_result_count=0,
                    only_landed_results=False,
                )),
            ) as provider:
                response = self.client.get("/flights/search/term", headers=self.headers(),
                                           params={"term": term, "app_version": "3.7"})
                self.assertEqual(response.status_code, 200)
                self.assertNotEqual(response.json().get("recovery", {}).get("reason"), "ai_consent_required")
                provider.assert_awaited_once()
        self.sdk.models.generate_content.assert_not_called()

    def test_verify_email_requires_current_owner_and_matching_apple_subject(self):
        for payload_owner, sub in ((OTHER, "apple-owner"), (OWNER, "apple-other")):
            with self.subTest(owner=payload_owner, sub=sub), patch.object(ai_consent, "verify_apple_identity_token", new=AsyncMock(
                return_value={"sub": sub, "email": EMAIL, "email_verified": True},
            )):
                response = self.client.post("/users/me/ai-consent/verify-email", headers=self.headers(),
                                            json={"user_id": payload_owner, "apple_jwt": SECRET})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"]["code"], "ai_consent_account_mismatch")
        self.assertFalse(self.snapshot().forwarded_email_verified)

    def test_verify_email_never_accepts_unverified_missing_or_numeric_true_claim(self):
        for verified in (False, "false", None, 1):
            with self.subTest(verified=verified), patch.object(ai_consent, "verify_apple_identity_token", new=AsyncMock(
                return_value={"sub": "apple-owner", "email": EMAIL, "email_verified": verified},
            )):
                response = self.client.post("/users/me/ai-consent/verify-email", headers=self.headers(),
                                            json={"user_id": OWNER, "apple_jwt": SECRET})
                self.assertEqual(response.status_code, 409)
                self.assertFalse(self.snapshot().forwarded_email_verified)
        with patch.object(ai_consent, "verify_apple_identity_token", new=AsyncMock(
            return_value={"sub": "apple-owner", "email_verified": True},
        )):
            response = self.client.post("/users/me/ai-consent/verify-email", headers=self.headers(),
                                        json={"user_id": OWNER, "apple_jwt": SECRET})
            self.assertEqual(response.status_code, 409)

    def test_verify_email_success_creates_proof_not_consent_and_unchanged_proof_is_idempotent(self):
        with patch.object(ai_consent, "verify_apple_identity_token", new=AsyncMock(return_value={
            "sub": "apple-owner", "email": EMAIL.upper(), "email_verified": "true",
        })):
            first = self.client.post("/users/me/ai-consent/verify-email", headers=self.headers(),
                                     json={"user_id": OWNER, "apple_jwt": SECRET})
            second = self.client.post("/users/me/ai-consent/verify-email", headers=self.headers(),
                                      json={"user_id": OWNER, "apple_jwt": SECRET})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertTrue(first.json()["forwarded_email_verified"])
        self.assertFalse(first.json()["forwarded_email_enabled"])
        self.assertEqual(self.session.get(UserAIEmailIdentity, OWNER).verified_email, EMAIL)

    def test_verify_endpoint_does_not_sign_in_guest_or_switch_account(self):
        self.session.add(User(id="guest"))
        self.session.commit()
        with patch.object(ai_consent, "verify_apple_identity_token", new=AsyncMock()) as verifier:
            response = self.client.post("/users/me/ai-consent/verify-email", headers=self.headers("guest"),
                                        json={"user_id": "guest", "apple_jwt": SECRET})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "ai_consent_sign_in_required")
        verifier.assert_not_called()
        self.assertFalse(self.session.get(User, "guest").verified)

    def test_apple_verification_error_never_echoes_secret_or_traceback(self):
        request = ai_consent.AIEmailVerificationRequest(user_id=OWNER, apple_jwt=SECRET)
        self.assertNotIn(SECRET, repr(request))
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logging.getLogger().addHandler(handler)
        try:
            with patch.object(ai_consent, "verify_apple_identity_token", new=AsyncMock(side_effect=RuntimeError(SECRET))):
                response = self.client.post("/users/me/ai-consent/verify-email", headers=self.headers(),
                                            json={"user_id": OWNER, "apple_jwt": SECRET})
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(SECRET, response.text)
        for record in records:
            self.assertNotIn(SECRET, repr(record.__dict__))
            self.assertFalse(record.exc_info)

    def test_account_identity_changed_during_apple_await_cannot_commit_proof(self):
        async def verify(_):
            with Session(self.engine) as separate:
                separate.exec(update(User).where(User.id == OWNER).values(apple_id="changed-subject"))
                separate.commit()
            return {"sub": "apple-owner", "email": EMAIL, "email_verified": True}

        with patch.object(ai_consent, "verify_apple_identity_token", new=verify):
            response = self.client.post("/users/me/ai-consent/verify-email", headers=self.headers(),
                                        json={"user_id": OWNER, "apple_jwt": SECRET})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.snapshot().forwarded_email_verified)

    def test_apple_signed_email_not_client_email_establishes_proof_on_normal_sign_in(self):
        self.session.add(User(id="guest"))
        self.session.commit()
        guest = self.session.get(User, "guest")
        with patch.object(users, "verify_apple_identity_token", new=AsyncMock(return_value={
            "sub": "new-apple-subject", "email": EMAIL, "email_verified": True,
        })):
            response = asyncio.run(users.create_user(users.CreateUserRequest(
                apple_jwt=SECRET, email="attacker-supplied@example.invalid",
            ), user=guest, session=self.session))
        self.assertEqual(response.user_id, "guest")
        self.assertEqual(self.session.get(UserAIEmailIdentity, "guest").verified_email, EMAIL)
        self.assertNotEqual(self.session.get(User, "guest").email, EMAIL)
        self.assertFalse(self.snapshot("guest").forwarded_email_enabled)

    def test_normal_sign_in_existing_account_proof_keeps_original_account_uuid(self):
        self.session.add(User(id="guest"))
        self.session.commit()
        with patch.object(users, "verify_apple_identity_token", new=AsyncMock(return_value={
            "sub": "apple-owner", "email": EMAIL, "email_verified": True,
        })):
            response = asyncio.run(users.create_user(users.CreateUserRequest(apple_jwt=SECRET),
                                                     user=self.session.get(User, "guest"), session=self.session))
        self.assertEqual(response.user_id, OWNER)
        self.assertTrue(self.snapshot().forwarded_email_verified)
        self.assertFalse(self.snapshot("guest").forwarded_email_verified)

    def test_changed_verified_sender_revokes_old_grant_and_conflicts_delayed_allow(self):
        self.prove_email()
        self.change(purpose="forwarded_email")
        pending = self.command(purpose="forwarded_email")
        self.prove_email(email="new-address@example.invalid")
        self.assertFalse(self.snapshot().forwarded_email_enabled)
        with self.assertRaises(AIConsentConflict):
            update_ai_consent(self.session, pending)

    def test_account_deletion_removes_consent_receipts_and_proof_and_recreation_denies(self):
        self.prove_email()
        self.change()
        self.change(purpose="forwarded_email")
        users.delete_user(session=self.session, user=self.owner)
        for model in (UserAIConsent, AIConsentReceipt, UserAIEmailIdentity):
            self.assertEqual(self.session.exec(select(model)).all(), [])
        self.session.add(User(id=OWNER))
        self.session.commit()
        self.assertFalse(self.snapshot().search_enabled)
        with self.assertRaises(AIConsentRequired):
            require_ai_consent(OWNER, "forwarded_email")

    def test_write_for_deleted_account_is_rejected_even_with_stale_authenticated_object(self):
        command = self.command()
        users.delete_user(session=self.session, user=self.owner)
        with self.assertRaises(AIConsentAccountUnavailable):
            update_ai_consent(self.session, command)
        self.assertEqual(self.session.exec(select(UserAIConsent)).all(), [])

    def deliver_email(self, *, sender=EMAIL, pdf=False):
        message = EmailMessage()
        message["From"] = sender
        message["To"] = "track@sofly.to"
        message.set_content(PRIVATE)
        if pdf:
            import fitz
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "PRIVATE PDF ITINERARY")
            message.add_attachment(document.tobytes(), maintype="application", subtype="pdf", filename="booking.pdf")
            document.close()
        s3 = MagicMock()
        data = message.as_bytes()
        s3.get_object.return_value = s3_object(data)
        with patch.object(background_tasks, "get_s3_client", return_value=s3), \
                patch.object(settings, "FORWARDED_EMAIL_BUCKET", BUCKET):
            asyncio.run(background_tasks.handle_incoming_email(notification(data, sender=sender)))

    def test_email_and_pdf_never_egress_for_legacy_search_only_or_revoked_accounts(self):
        for state in ("legacy", "search_only", "revoked"):
            with self.subTest(state=state):
                if state == "search_only":
                    self.prove_email()
                    self.change()
                elif state == "revoked":
                    self.change(purpose="forwarded_email")
                    self.change(purpose="forwarded_email", enabled=False)
                self.deliver_email(pdf=True)
        self.sdk.models.generate_content.assert_not_called()

    def test_email_worker_uses_signed_identity_not_legacy_email_and_ambiguous_senders_deny(self):
        self.prove_email(email="proven-different@example.invalid")
        self.change(purpose="forwarded_email")
        self.deliver_email(sender=EMAIL)
        self.sdk.models.generate_content.assert_not_called()
        self.prove_email(email=EMAIL)
        self.change(purpose="forwarded_email")
        self.prove_email(owner=OTHER, email=EMAIL)
        self.deliver_email(sender=EMAIL)
        self.sdk.models.generate_content.assert_not_called()

    def test_explicit_email_consent_can_send_body_and_pdf_text_to_stubbed_sdk(self):
        self.prove_email()
        self.change(purpose="forwarded_email")
        self.deliver_email(pdf=True)
        self.assertEqual(self.sdk.models.generate_content.call_count, 3)
        contents = self.sdk.models.generate_content.call_args.kwargs["contents"]
        self.assertIn(PRIVATE, contents)
        self.assertIn("PRIVATE PDF ITINERARY", contents)

    def test_email_revocation_after_initial_check_before_sdk_stops_send(self):
        self.prove_email()
        self.change(purpose="forwarded_email")

        def checked_then_revoked(context):
            require_email_consent_for_send(context)
            self.change(purpose="forwarded_email", enabled=False)

        with patch.object(background_tasks, "require_email_consent_for_send", side_effect=checked_then_revoked):
            self.deliver_email(pdf=True)
        self.sdk.models.generate_content.assert_not_called()

    def test_changed_sender_then_new_grant_cannot_authorize_old_email_on_retry(self):
        self.prove_email()
        self.change(purpose="forwarded_email")

        def first_send(**kwargs):
            self.prove_email(email="new-sender@example.invalid")
            self.change(purpose="forwarded_email")
            raise RuntimeError(PRIVATE)

        self.sdk.models.generate_content.side_effect = first_send
        self.deliver_email(pdf=True)
        self.assertEqual(self.sdk.models.generate_content.call_count, 1)
        self.assertTrue(self.snapshot().forwarded_email_enabled)

    def test_email_sdk_boundary_requires_frozen_sender_even_when_email_scope_enabled(self):
        self.prove_email()
        self.change(purpose="forwarded_email")
        for sender in (None, "wrong-sender@example.invalid"):
            with self.subTest(sender=sender), self.assertRaises(AIConsentRequired):
                asyncio.run(GeminiService(user_id=OWNER, email_sender=sender)._generate(
                    PRIVATE, email_config, purpose="forwarded_email",
                ))
        self.sdk.models.generate_content.assert_not_called()

    def test_migration_matches_fresh_schema_and_retries_without_granting_legacy_accounts(self):
        self.assertFalse(migrate(self.database))
        self.assertFalse(self.snapshot().search_enabled)
        legacy = Path(self.scratch.name) / "legacy.db"
        with sqlite3.connect(legacy) as connection:
            connection.execute("CREATE TABLE user (id VARCHAR PRIMARY KEY, email VARCHAR, verified BOOLEAN)")
            connection.execute("INSERT INTO user VALUES (?, ?, 1)", (OWNER, EMAIL))
        self.assertTrue(migrate(legacy))
        self.assertFalse(migrate(legacy))
        with sqlite3.connect(legacy) as connection:
            self.assertEqual(connection.execute("SELECT id, email, verified FROM user").fetchall(), [(OWNER, EMAIL, 1)])
            for table in ("useraiconsent", "aiconsentreceipt", "useraiemailidentity"):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            connection.execute("INSERT INTO useraiconsent (user_id) VALUES (?)", (OWNER,))
            self.assertEqual(connection.execute(
                "SELECT policy_version, revision, search_enabled, forwarded_email_enabled FROM useraiconsent"
            ).fetchone(), (1, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
