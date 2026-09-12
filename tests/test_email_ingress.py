"""Synthetic-only SES, storage, HTTP, concurrency and actual SDK-egress tests."""

import asyncio
import copy
import importlib.util
import io
import json
import logging
import os
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from tests import test_experiment_reporting as _test_environment

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine, select, update

from core import background_tasks
from core import email_ingress_contract as contract
from core.config import settings
from core.models import get_session
from core.models.ai_consent import UserAIConsent, UserAIEmailIdentity, UserAIEmailReceipt
from core.models.email import S3EmailNotification
from core.models.flight import Flight, UserFlightLink
from core.models.user import User
from core.routers import ai_consent, incoming_email, users
from core.services.ai_consent import (
    AIConsentRequired, AIConsentUnavailable, AIConsentUpdate, read_ai_consent,
    record_verified_apple_email_identity, update_ai_consent,
)
from core.services.email_ingress import (
    claim_email_receipt, finish_email_receipt, require_email_consent_for_send,
)
from core.services.gemini.config import FunctionDefinition, email_config
from core.services.gemini.service import GeminiService
from core.utils import create_jwt
from tests.email_ingress_fixtures import (
    BUCKET, CONFIG, FUNCTION_ARN, RECIPIENT, SENDER, etag, notification, raw_email,
    s3_object, ses_event,
)


SOURCE = Path(__file__).resolve().parents[1] / "lambda/lambda_function.py"
spec = importlib.util.spec_from_file_location("synthetic_ses_lambda", SOURCE)
LAMBDA = importlib.util.module_from_spec(spec)
with patch.dict("sys.modules", {"email_ingress_contract": contract}):
    spec.loader.exec_module(LAMBDA)

OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
SECRET = "SYNTHETIC_LAMBDA_CREDENTIAL_NOT_FOR_LOGS"
PRIVATE = "PRIVATE_SYNTHETIC_ITINERARY_NOT_FOR_LOGS"
ENV = {
    "BACKEND_URL": "https://api.sofly.to", "LAMBDA_FUNCTION_AUTH_TOKEN": SECRET,
    "FORWARDED_EMAIL_BUCKET": BUCKET, "FORWARDED_EMAIL_KEY_PREFIX": contract.DEFAULT_KEY_PREFIX,
    "FORWARDED_EMAIL_RECIPIENT": RECIPIENT, "FORWARDED_EMAIL_LAMBDA_ARN": FUNCTION_ARN,
}


def response(status=202):
    result = MagicMock()
    result.__enter__.return_value = result
    result.status = status
    return result


class SESContractTests(unittest.TestCase):
    def extract(self, event, **kwargs):
        return contract.extract_ses_receipt(event, CONFIG, function_arn=FUNCTION_ARN,
                                           invoked_function_arn=FUNCTION_ARN, **kwargs)

    def test_uses_ses_message_id_and_received_time_not_mime_id_or_action_time(self):
        received = contract.now_ms() - 10_000
        event = ses_event(received_at_ms=received, action_delay_ms=1250)
        proof = self.extract(event)
        self.assertEqual(proof["received_at_ms"], received)
        self.assertEqual(proof["message_id"], event["Records"][0]["ses"]["mail"]["messageId"])
        self.assertNotIn("attacker-mime", json.dumps(proof))

    def test_dmarc_scan_missing_failed_gray_and_malformed_verdicts_fail_closed(self):
        for field in ("dmarcVerdict", "spamVerdict", "virusVerdict"):
            for value in (None, {}, {"status": "FAIL"}, {"status": "GRAY"}, {"status": "PROCESSING_FAILED"}, {"status": True}):
                with self.subTest(field=field, value=value):
                    event = ses_event()
                    event["Records"][0]["ses"]["receipt"][field] = value
                    with self.assertRaises(contract.EmailIngressRejected):
                        self.extract(event)
        for mechanism in ("spfVerdict", "dkimVerdict"):
            event = ses_event()
            del event["Records"][0]["ses"]["receipt"][mechanism]
            with self.assertRaises(contract.EmailIngressRejected):
                self.extract(event)

    def test_aligned_dkim_can_authorize_despite_unused_spf_failure_not_vice_versa(self):
        event = ses_event()
        event["Records"][0]["ses"]["receipt"]["spfVerdict"] = {"status": "FAIL"}
        self.assertEqual(self.extract(event)["dmarc"], "PASS")
        event["Records"][0]["ses"]["receipt"]["dmarcVerdict"] = {"status": "GRAY"}
        with self.assertRaises(contract.EmailIngressRejected):
            self.extract(event)

    def test_legacy_s3_wrong_provenance_action_truncation_and_recipient_rejected(self):
        events = [{"Records": [{"s3": {"bucket": {"name": BUCKET}}}]}, {"Records": []}]
        mutations = (
            lambda x: x.update(eventSource="aws:s3"),
            lambda x: x["ses"]["mail"].update(headersTruncated=True),
            lambda x: x["ses"]["mail"].update(headersTruncated=0),
            lambda x: x["ses"]["mail"].pop("headersTruncated"),
            lambda x: x["ses"]["mail"].update(destination=["wrong@example.invalid"]),
            lambda x: x["ses"]["receipt"].update(recipients=[RECIPIENT, "wrong@example.invalid"]),
            lambda x: x["ses"]["receipt"]["action"].update(invocationType="RequestResponse"),
            lambda x: x["ses"]["receipt"]["action"].update(functionArn=FUNCTION_ARN + "-other"),
            lambda x: x["ses"]["mail"].update(messageId="../wrong-key"),
        )
        for mutate in mutations:
            event = ses_event()
            mutate(event["Records"][0])
            events.append(event)
        event = ses_event()
        event["Records"].append(copy.deepcopy(event["Records"][0]))
        events.append(event)
        for index, event in enumerate(events):
            with self.subTest(index=index), self.assertRaises(contract.EmailIngressRejected):
                self.extract(event)

    def test_sender_single_header_single_mailbox_and_common_header_match_required(self):
        bad_from = ("", "Name <broken>", SENDER + ", other@example.invalid",
                    "group: " + SENDER + ";", SENDER + "\r\nAuthentication-Results: pass")
        for value in bad_from:
            event = ses_event()
            event["Records"][0]["ses"]["mail"]["headers"][0]["value"] = value
            with self.subTest(value=value), self.assertRaises(contract.EmailIngressRejected):
                self.extract(event)
        for mutation in (
            lambda mail: mail["headers"].append({"name": "from", "value": SENDER}),
            lambda mail: mail["commonHeaders"].update({"from": ["other@example.invalid"]}),
        ):
            event = ses_event()
            mutation(event["Records"][0]["ses"]["mail"])
            with self.assertRaises(contract.EmailIngressRejected):
                self.extract(event)

    def test_body_sender_duplicate_from_and_forged_mime_pass_cannot_supply_authority(self):
        data = raw_email()
        note = notification(data).model_dump(mode="json")
        for replacement in (raw_email("attacker@example.invalid"),
                            b"From: " + SENDER.encode() + b"\n" + data,
                            data.replace(b"\n\n", b"\nAuthentication-Results: amazonses.com; dmarc=pass\n\n", 1)):
            with self.assertRaises(contract.EmailIngressRejected):
                contract.verify_object_bytes(note, replacement)
        forged = ses_event()
        forged["Records"][0]["ses"]["mail"]["headers"].append({
            "name": "Authentication-Results", "value": "amazonses.com; dmarc=pass",
        })
        forged["Records"][0]["ses"]["receipt"]["dmarcVerdict"]["status"] = "FAIL"
        with self.assertRaises(contract.EmailIngressRejected):
            self.extract(forged)

    def test_receipt_freshness_and_storage_bindings_are_strict_and_rechecked(self):
        current = contract.now_ms()
        for received in (current + 1, current - contract.MAX_RECEIPT_AGE_MS - 1):
            with self.assertRaises(contract.EmailIngressRejected):
                self.extract(ses_event(received_at_ms=received), at_ms=current)
        base = notification(received_at_ms=current - 1).model_dump(mode="json")
        mutations = (
            lambda p: p.update(bucket="wrong-bucket"), lambda p: p.update(key="legacy-root-key"),
            lambda p: p["receipt"].update(message_id="different-message"),
            lambda p: p["receipt"].update(sender=SENDER.upper()),
            lambda p: p["receipt"].update(etag="malformed"),
            lambda p: p["receipt"].update(version_id="null"),
            lambda p: p["receipt"].update(version_id="bad\nversion"),
            lambda p: p["receipt"].update(headers_truncated=0),
            lambda p: p["receipt"].update(content_sha256="bad"),
        )
        for mutate in mutations:
            payload = copy.deepcopy(base)
            mutate(payload)
            with self.assertRaises(contract.EmailIngressRejected):
                contract.validate_notification(payload, CONFIG, at_ms=current)
        with self.assertRaises(contract.EmailIngressRejected):
            contract.validate_notification(base, CONFIG, at_ms=current + contract.MAX_RECEIPT_AGE_MS)


class LambdaTransportTests(unittest.TestCase):
    def invoke(self, event=None, data=None, opener=None, obj=None):
        data = raw_email() if data is None else data
        self.s3 = MagicMock()
        self.s3.get_object.return_value = s3_object(data) if obj is None else obj
        self.opener = opener or MagicMock()
        if opener is None:
            self.opener.open.return_value = response()
        with patch.dict(os.environ, ENV, clear=True), patch.object(LAMBDA.boto3, "client", return_value=self.s3), \
                patch.object(LAMBDA.urllib.request, "build_opener", return_value=self.opener):
            return LAMBDA.lambda_handler(event or ses_event(), SimpleNamespace(invoked_function_arn=FUNCTION_ARN))

    def test_valid_ses_only_reads_configured_key_and_sends_bounded_bound_proof(self):
        event = ses_event()
        result = self.invoke(event=event)
        self.assertEqual(result["statusCode"], 202)
        identifier = event["Records"][0]["ses"]["mail"]["messageId"]
        self.s3.get_object.assert_called_once_with(Bucket=BUCKET, Key=contract.DEFAULT_KEY_PREFIX + identifier)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.sofly.to/emails/")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
        payload = json.loads(request.data)
        contract.validate_notification(payload, CONFIG)
        self.assertNotIn(PRIVATE, request.data.decode())
        self.assertNotIn(SECRET, request.data.decode())

    def test_success_log_is_enabled_on_module_only_and_contains_no_payload(self):
        self.assertEqual(LAMBDA.logger.level, logging.INFO)
        root_level = logging.getLogger().level
        sdk_level = logging.getLogger("botocore").level
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        LAMBDA.logger.addHandler(handler)
        try:
            with patch.object(LAMBDA.logger, "propagate", False):
                self.assertEqual(self.invoke()["statusCode"], 202)
        finally:
            LAMBDA.logger.removeHandler(handler)
            handler.close()
        self.assertEqual(stream.getvalue(), "lambda_email_intake_accepted\n")
        self.assertEqual(logging.getLogger().level, root_level)
        self.assertEqual(logging.getLogger("botocore").level, sdk_level)

    def test_legacy_or_invalid_event_never_reads_storage_or_posts(self):
        event = {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": PRIVATE}}}]}
        self.assertEqual(self.invoke(event=event)["statusCode"], 403)
        self.s3.get_object.assert_not_called()
        self.opener.open.assert_not_called()

    def test_changed_truncated_oversized_or_wrong_sender_object_never_posts(self):
        data = raw_email()
        objects = (s3_object(raw_email("wrong@example.invalid")),
                   dict(s3_object(data), ContentLength=len(data) + 1),
                   dict(s3_object(data), ContentLength=contract.MAX_EMAIL_BYTES + 1),
                   dict(s3_object(data), ETag="unbounded-etag"))
        for obj in objects:
            with self.subTest(metadata={k: v for k, v in obj.items() if k != "Body"}):
                self.assertEqual(self.invoke(obj=obj)["statusCode"], 403)
                self.opener.open.assert_not_called()
                self.assertTrue(obj["Body"].closed)

    def test_transient_http_and_storage_failures_raise_only_bounded_retry_errors(self):
        for code in (408, 429, 500, 503):
            opener = MagicMock()
            opener.open.side_effect = urllib.error.HTTPError("https://api.sofly.to/emails/", code, SECRET, {}, io.BytesIO(PRIVATE.encode()))
            with self.subTest(code=code), self.assertRaisesRegex(RuntimeError, "^Email intake delivery failed$") as caught:
                self.invoke(opener=opener)
            self.assertTrue(caught.exception.__suppress_context__)
        broken = MagicMock()
        broken.read.side_effect = RuntimeError(SECRET + PRIVATE)
        with self.assertRaisesRegex(RuntimeError, "^Email intake delivery failed$"):
            self.invoke(obj={"Body": broken, "ContentLength": 1, "ETag": etag(b"x")})
        broken.close.assert_called_once()

    def test_permanent_http_rejection_is_bounded_and_does_not_retry(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("https://api.sofly.to/emails/", 403, SECRET, {}, io.BytesIO(PRIVATE.encode()))
        result = self.invoke(opener=opener)
        self.assertEqual(result, {"statusCode": 403, "body": "email_intake_rejected"})

    def test_http_error_close_failure_cannot_escape_with_private_details(self):
        opener = MagicMock()
        error = urllib.error.HTTPError("https://api.sofly.to/emails/", 403, SECRET, {}, None)
        error.close = MagicMock(side_effect=RuntimeError(SECRET + PRIVATE))
        opener.open.side_effect = error
        with self.assertRaisesRegex(RuntimeError, "^Email intake delivery failed$") as caught:
            self.invoke(opener=opener)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_redirect_handler_blocks_every_redirect_without_followup_request(self):
        request = LAMBDA.urllib.request.Request("https://api.sofly.to/emails/", data=b"{}",
                                              headers={"Authorization": "Bearer " + SECRET}, method="POST")
        handler = LAMBDA.NoRedirect()
        for code in (301, 302, 303, 307, 308):
            for target in ("https://attacker.example.invalid/capture", "http://api.sofly.to/emails/", "https://api.sofly.to/other"):
                self.assertIsNone(handler.redirect_request(request, None, code, "redirect", {}, target))


class EmailIngressTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="sofly-ses-ingress-")
        self.engine = create_engine(f"sqlite:///{self.scratch.name}/fixture.db", hide_parameters=True,
                                   connect_args={"check_same_thread": False, "timeout": 10})
        SQLModel.metadata.create_all(self.engine)
        self.patchers = [patch("core.models.engine", self.engine), patch.object(background_tasks, "engine", self.engine),
                         patch.object(settings, "FORWARDED_EMAIL_BUCKET", BUCKET)]
        for patcher in self.patchers:
            patcher.start()
        self.sdk = MagicMock()
        self.sdk.models.generate_content.return_value = None
        self.sdk_patcher = patch("core.services.gemini.service.genai.Client", return_value=self.sdk)
        self.sdk_patcher.start()
        self.started = contract.now_ms() - 5000
        with Session(self.engine) as session:
            session.add(User(id=OWNER, apple_id="apple-owner", email=SENDER, verified=True))
            session.add(User(id=OTHER, apple_id="apple-other", email="other@example.invalid", verified=True))
            session.add(Flight(id=42, date="2026-09-12", number="AA100", status="Expected"))
            session.commit()
            record_verified_apple_email_identity(session, OWNER, {
                "sub": "apple-owner", "email": SENDER, "email_verified": True,
            })
            session.commit()
        self.grant(at_ms=self.started)
        app = FastAPI()
        app.include_router(incoming_email.router, prefix="/emails")
        app.include_router(ai_consent.router, prefix="/users")
        def dependency():
            with Session(self.engine) as session:
                yield session
        app.dependency_overrides[get_session] = dependency
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.sdk_patcher.stop()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.engine.dispose()
        self.scratch.cleanup()

    def grant(self, *, enabled=True, at_ms=None):
        with Session(self.engine) as session:
            current = read_ai_consent(session, OWNER)
            result = update_ai_consent(session, AIConsentUpdate(
                user_id=OWNER, policy_version=1, expected_revision=current.revision,
                purpose="forwarded_email", enabled=enabled, request_id=uuid4(),
            ))
            if at_ms is not None and enabled:
                session.exec(update(UserAIConsent).where(UserAIConsent.user_id == OWNER).values(
                    forwarded_email_granted_at_ms=at_ms,
                ))
                session.commit()
            return result

    def deliver(self, note, data, *, sdk_result=None):
        s3 = MagicMock()
        s3.get_object.side_effect = lambda **_: s3_object(data, version_id=note.receipt.version_id)
        with patch.object(background_tasks, "get_s3_client", return_value=s3):
            asyncio.run(background_tasks.handle_incoming_email(note))
        return s3

    def test_old_s3_request_missing_auth_and_user_bearer_never_enqueue_or_echo(self):
        payload = notification().model_dump(mode="json")
        for headers in ({}, {"Authorization": "Bearer " + create_jwt(sub=OWNER)}, {"Authorization": "Bearer wrong"}):
            with patch.object(incoming_email, "handle_incoming_email") as worker:
                result = self.client.post("/emails/", headers=headers, json=payload)
                self.assertIn(result.status_code, (401, 403))
                worker.assert_not_called()
        result = self.client.post("/emails/", headers={"Authorization": "Bearer test"}, json={"bucket": PRIVATE, "key": SECRET})
        self.assertEqual(result.status_code, 422)
        self.assertNotIn(PRIVATE, result.text)
        self.assertNotIn(SECRET, result.text)

    def test_dto_and_http_reject_coercions_unknown_fields_wrong_binding_and_config(self):
        for field, value in (("version", True), ("received_at_ms", True), ("headers_truncated", 0), ("sender", 42)):
            payload = notification().model_dump(mode="json")
            payload["receipt"][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                S3EmailNotification.model_validate(payload)
        payload = notification().model_dump(mode="json")
        payload["receipt"]["private_body"] = PRIVATE
        result = self.client.post("/emails/", headers={"Authorization": "Bearer test"}, json=payload)
        self.assertEqual(result.status_code, 422)
        self.assertNotIn(PRIVATE, result.text)
        payload = notification().model_dump(mode="json")
        payload["key"] = "wrong-key"
        result = self.client.post("/emails/", headers={"Authorization": "Bearer test"}, json=payload)
        self.assertEqual(result.status_code, 403)
        with patch.object(settings, "FORWARDED_EMAIL_BUCKET", ""):
            result = self.client.post("/emails/", headers={"Authorization": "Bearer test"}, json=notification().model_dump(mode="json"))
        self.assertEqual(result.status_code, 503)

    def test_mail_before_or_equal_grant_is_denied_and_later_allow_cannot_authorize_it(self):
        for timestamp in (self.started - 1, self.started):
            note = notification(received_at_ms=timestamp)
            with self.assertRaises(AIConsentRequired):
                claim_email_receipt(note)
            self.grant(enabled=False)
            self.grant()
            with self.assertRaises(AIConsentRequired):
                claim_email_receipt(note)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(UserAIEmailReceipt)).all(), [])

    def test_concurrent_duplicate_workers_claim_one_sdk_job_and_replays_do_not_reclaim(self):
        note = notification()
        barrier = threading.Barrier(2)
        def claim(_):
            barrier.wait(timeout=5)
            return claim_email_receipt(note)
        with ThreadPoolExecutor(max_workers=2) as pool:
            contexts = list(pool.map(claim, range(2)))
        self.assertEqual(sum(context is not None for context in contexts), 1)
        context = next(value for value in contexts if value is not None)
        service = GeminiService(user_id=OWNER, email_sender=SENDER, email_receipt=context)
        asyncio.run(service._generate(PRIVATE, email_config, purpose="forwarded_email"))
        self.assertEqual(self.sdk.models.generate_content.call_count, 1)
        # Simulated process loss before finalization: no lease/reclaim or blind SDK retry.
        self.assertIsNone(claim_email_receipt(note))
        finish_email_receipt(context, "completed")
        self.assertIsNone(claim_email_receipt(note))
        with self.assertRaises(AIConsentRequired):
            require_email_consent_for_send(context)

    def test_same_receipt_mutated_bytes_or_proof_is_not_a_fresh_job(self):
        original = notification()
        claim_email_receipt(original)
        changed = notification(raw_email(body=PRIVATE), message_id=original.receipt.message_id,
                               received_at_ms=original.receipt.received_at_ms)
        with self.assertRaises(contract.EmailIngressRejected):
            claim_email_receipt(changed)

    def test_claimed_receipt_owner_sender_and_generation_cannot_be_rebound(self):
        context = claim_email_receipt(notification())
        for altered in (replace(context, user_id=OTHER), replace(context, grant_id=str(uuid4())),
                        replace(context, proof_digest="0" * 64)):
            with self.assertRaises(AIConsentRequired):
                require_email_consent_for_send(altered)
        with Session(self.engine) as session:
            record_verified_apple_email_identity(session, OTHER, {
                "sub": "apple-other", "email": SENDER, "email_verified": True,
            })
            session.commit()
        with self.assertRaises(AIConsentRequired):
            require_email_consent_for_send(context)

    def test_revoke_regrant_account_change_and_receipt_age_checked_at_each_sdk_retry(self):
        for mutation in ("revoke", "regrant", "subject", "expired"):
            with self.subTest(mutation=mutation):
                with Session(self.engine) as session:
                    session.exec(update(User).where(User.id == OWNER).values(apple_id="apple-owner"))
                    session.commit()
                self.grant(at_ms=self.started)
                context = claim_email_receipt(notification())
                service = GeminiService(user_id=OWNER, email_sender=SENDER, email_receipt=context)
                def first_send(**kwargs):
                    if mutation in ("revoke", "regrant"):
                        self.grant(enabled=False)
                        if mutation == "regrant":
                            self.grant(at_ms=self.started)
                    elif mutation == "subject":
                        with Session(self.engine) as session:
                            session.exec(update(User).where(User.id == OWNER).values(apple_id="changed"))
                            session.commit()
                    else:
                        # Recheck exact receipt lifetime immediately before send.
                        patcher = patch.object(contract, "now_ms", return_value=context.notification.receipt.received_at_ms + contract.MAX_RECEIPT_AGE_MS + 1)
                        patcher.start()
                        self.addCleanup(patcher.stop)
                    raise RuntimeError(PRIVATE)
                self.sdk.models.generate_content.reset_mock(side_effect=True)
                self.sdk.models.generate_content.side_effect = first_send
                with self.assertRaises(AIConsentRequired):
                    asyncio.run(service.get_function_call(PRIVATE, email=True))
                self.assertEqual(self.sdk.models.generate_content.call_count, 1)

    def test_deleted_account_does_not_delete_receipt_tombstone_or_authorize_egress(self):
        note = notification()
        context = claim_email_receipt(note)
        with Session(self.engine) as session:
            users.delete_user(user=session.get(User, OWNER), session=session)
            self.assertIsNotNone(session.get(UserAIEmailReceipt, context.receipt_digest))
        with self.assertRaises(AIConsentRequired):
            require_email_consent_for_send(context)
        self.assertIsNone(claim_email_receipt(note))

    def test_object_ifmatch_version_digest_and_actual_sender_checked_before_sdk(self):
        data = raw_email()
        note = notification(data, version_id="immutable-fixture-version")
        s3 = self.deliver(note, data)
        s3.get_object.assert_called_once_with(Bucket=BUCKET, Key=note.key, IfMatch=note.receipt.etag,
                                             VersionId="immutable-fixture-version")
        self.assertEqual(self.sdk.models.generate_content.call_count, 3)
        self.sdk.models.generate_content.reset_mock()
        for changed in (data + PRIVATE.encode(), raw_email("other@example.invalid")):
            self.deliver(notification(data), changed)
        self.sdk.models.generate_content.assert_not_called()

    def test_store_failure_is_fail_closed_without_raw_driver_exception(self):
        context = claim_email_receipt(notification())
        with patch("core.services.email_ingress.Session", side_effect=RuntimeError(PRIVATE + SECRET)):
            with self.assertRaisesRegex(AIConsentUnavailable, "^Email consent unavailable$"):
                require_email_consent_for_send(context)
        self.sdk.models.generate_content.assert_not_called()

    def test_older_verification_completion_cannot_replace_new_proof_or_new_allow(self):
        async def verify(_):
            with Session(self.engine) as session:
                record_verified_apple_email_identity(session, OWNER, {
                    "sub": "apple-owner", "email": "new-proof@example.invalid", "email_verified": True,
                })
                session.commit()
            self.grant()
            return {"sub": "apple-owner", "email": SENDER, "email_verified": True}
        with patch.object(ai_consent, "verify_apple_identity_token", new=verify):
            result = self.client.post("/users/me/ai-consent/verify-email",
                                      headers={"Authorization": "Bearer " + create_jwt(sub=OWNER)},
                                      json={"user_id": OWNER, "apple_jwt": SECRET})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["detail"]["code"], "ai_consent_conflict")
        with Session(self.engine) as session:
            self.assertEqual(session.get(UserAIEmailIdentity, OWNER).verified_email, "new-proof@example.invalid")
            self.assertTrue(read_ai_consent(session, OWNER).forwarded_email_enabled)

    def test_lambda_to_authenticated_http_to_real_sdk_boundary_and_save_succeeds_once(self):
        data = raw_email(body=PRIVATE)
        self.sdk.models.generate_content.return_value = SimpleNamespace(candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(function_call=SimpleNamespace(
                name="extract_flight_from_email", args={"flight_number": "100", "airline_iata": "AA", "departure_date": "2026-09-12"},
            ))]),
        )])
        async def handler(*, session, **kwargs):
            return [session.get(Flight, 42)]
        opener = MagicMock()
        def deliver_request(request, **kwargs):
            result = self.client.post("/emails/", headers={"Authorization": request.get_header("Authorization")},
                                      content=request.data)
            self.assertEqual(result.status_code, 202)
            return response(result.status_code)
        opener.open.side_effect = deliver_request
        s3 = MagicMock()
        s3.get_object.side_effect = lambda **_: s3_object(data)
        with patch.dict(os.environ, ENV, clear=True), patch.object(settings, "LAMBDA_FUNCTION_AUTH_TOKEN", SECRET), \
                patch.object(LAMBDA.boto3, "client", return_value=s3), \
                patch.object(LAMBDA.urllib.request, "build_opener", return_value=opener), \
                patch.object(background_tasks, "get_s3_client", return_value=s3), \
                patch.dict("core.services.gemini.service.REQUIRED_FIELDS", {
                    "extract_flight_from_email": FunctionDefinition(
                        handler=handler, required_fields=["flight_number", "airline_iata", "departure_date"],
                    ),
                }):
            event = ses_event()
            self.assertEqual(LAMBDA.lambda_handler(event, SimpleNamespace(invoked_function_arn=FUNCTION_ARN))["statusCode"], 202)
            self.assertEqual(LAMBDA.lambda_handler(event, SimpleNamespace(invoked_function_arn=FUNCTION_ARN))["statusCode"], 202)
        self.assertEqual(self.sdk.models.generate_content.call_count, 1)
        self.assertIn(PRIVATE, self.sdk.models.generate_content.call_args.kwargs["contents"])
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(UserFlightLink).where(UserFlightLink.user_id == OWNER)).all()), 1)
            receipts = session.exec(select(UserAIEmailReceipt)).all()
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0].state, "completed")
            self.assertNotIn(SENDER, repr(receipts[0]))
            self.assertNotIn(event["Records"][0]["ses"]["mail"]["messageId"], repr(receipts[0]))


if __name__ == "__main__":
    unittest.main()
