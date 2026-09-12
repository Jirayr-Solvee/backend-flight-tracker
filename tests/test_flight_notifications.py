import os
import io
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


TEST_FILE = Path(__file__).resolve()
REPOSITORY_ROOT = TEST_FILE.parents[1]

for key, value in {
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_BUCKET_NAME": "test",
    "AWS_REGION": "eu-north-1",
    "LAMBDA_FUNCTION_AUTH_TOKEN": "test",
    "GEMINI_API_KEY": "test",
    "API_URL": "https://example.invalid",
    "JWT_SECRET": "test",
    "JWT_ALGORITHM": "HS256",
    "JWT_EXPIRE_DAYS": "1",
    "KEY_ID": "test",
    "ISSUER_ID": "test",
    "BUNDLE_ID": "com.zhirayr.Flight-tracker-shared",
    "APP_APPLE_ID": "1",
    "TEAM_ID": "test",
    "X_API_MARKET_KEY": "test",
    "AERODATABOX_SERVICE_URL": "https://example.invalid",
    "BALANCE_REFILL_AMMOUNT": "1",
    "BALANCE_REFILL_THRESHOLD": "1",
    "JWS_ENV": "XCODE",
    "MAX_PREMIUM_HOURS": "1",
    "APPLE_ISSUER": "test",
    "APPLE_KEYS_URL": "https://example.invalid",
    "GUEST_KEY": "test",
    "APN_KEY_PATH": str(TEST_FILE),
    "APPLE_ROOT_CERT_PATH": str(TEST_FILE),
    "AIRLINE_MAP_JSON": str(REPOSITORY_ROOT / "iata_to_icao.json"),
}.items():
    os.environ.setdefault(key, value)


from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from core import background_tasks
from core.models.aerodatabox import (
    AerodataboxOriginAndDestinationInformationWebhook,
)
from core.models.device import Device
from core.models.email import EmailRead, SESReceiptProof, S3EmailNotification
from core.models.flight import Departure, Flight
from core.models.notification import DeviceInfo, Notification, NotificationBatch
from core.models.user import User
from core.routers import users
from core.routers.webhook import partition_notification_refresh_tokens
from core.services.apn.service import ApnService
from core.services.apn.utils import (
    consolidate_notification_batches,
    extract_nested_notifications_for_flight,
)
from scripts.migrate_device_localized_push_version import migrate


def device(*, localized: bool = False, token: str = "token-1", version: int = 1) -> DeviceInfo:
    return DeviceInfo(
        token=token,
        badge=1,
        user_id="user-1",
        notification_count=0,
        supports_localized_push=localized,
        localized_push_version=version,
    )


class NotificationExtractionTests(unittest.TestCase):
    def test_provider_snapshot_is_consolidated_to_highest_priority_alert(self):
        devices = [device()]
        low = NotificationBatch(
            notification=Notification(
                title="Aircraft updated",
                body="Aircraft changed",
                flight_id=42,
                update_type="aircraft",
                priority=50,
            ),
            devices=devices,
            invoke_review=True,
        )
        high = NotificationBatch(
            notification=Notification(
                title="Gate updated",
                body="Gate B12",
                flight_id=42,
                update_type="gate",
                priority=110,
            ),
            devices=devices,
        )

        result = consolidate_notification_batches([low, high])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].notification.update_type, "gate")
        self.assertTrue(result[0].invoke_review)

    def test_retracted_provider_detail_does_not_create_none_alert(self):
        db_departure = Departure(gate="B12")
        webhook_departure = (
            AerodataboxOriginAndDestinationInformationWebhook.model_construct(
                terminal=None,
                checkInDesk=None,
                gate=None,
                baggageBelt=None,
                scheduledTime=None,
                predictedTime=None,
                revisedTime=None,
                runwayTime=None,
            )
        )

        result = extract_nested_notifications_for_flight(
            flight_id=42,
            flight_number="AA100",
            db_info=db_departure,
            webhook_data=webhook_departure,
            devices_info=[device()],
        )

        self.assertEqual(result, [])

    def test_review_eligible_token_wins_over_plain_refresh(self):
        normal_batch = NotificationBatch(
            notification=Notification(
                title="Updated",
                body="Updated",
                flight_id=42,
                update_type="time",
            ),
            devices=[device(token="shared"), device(token="normal")],
        )
        review_batch = NotificationBatch(
            notification=Notification(
                title="Useful update",
                body="Gate B12",
                flight_id=42,
                update_type="gate",
            ),
            devices=[device(token="shared"), device(token="review")],
            invoke_review=True,
        )

        refresh_tokens, review_tokens = partition_notification_refresh_tokens(
            [normal_batch, review_batch]
        )

        self.assertEqual(refresh_tokens, {"normal"})
        self.assertEqual(review_tokens, {"shared", "review"})


class AircraftNotificationLocalizationTests(unittest.TestCase):
    def test_missing_models_use_existing_generic_key_for_every_branch(self):
        for new_model in (None, "", " \t\n "):
            branches = (
                ("assigned", None, "N100QA", None,
                 f"Aircraft N100QA ({new_model or 'Unknown Model'}) has been assigned to your flight."),
                ("registration_changed", "N099QA", "N100QA", "Boeing 737",
                 f"Aircraft changed to N100QA ({new_model or 'Unknown Model'})."),
                ("model_changed", "N100QA", "N100QA", "Boeing 737",
                 f"The aircraft model for your flight N100QA has been updated to {new_model}."),
                ("generic", "N100QA", "N100QA", new_model,
                 "Aircraft information has been updated for flight AA100."),
            )
            for branch, old_reg, new_reg, old_model, legacy_body in branches:
                with self.subTest(branch=branch, new_model=new_model):
                    notification = ApnService.create_aircraft_updated_notification(
                        flight_id=42,
                        flight_number="AA100",
                        old_reg=old_reg,
                        new_reg=new_reg,
                        new_model=new_model,
                        old_model=old_model,
                    )

                    self.assertEqual(notification.title, "Aircraft updated for AA100")
                    self.assertEqual(notification.body, legacy_body)
                    self.assertEqual(notification.title_loc_key, "Aircraft updated for %@")
                    self.assertEqual(notification.title_loc_args, ["AA100"])
                    self.assertEqual(
                        notification.body_loc_key,
                        "Aircraft information has been updated for flight %@.",
                    )
                    self.assertEqual(notification.body_loc_args, ["AA100"])
                    self.assertNotIn("unknown model", " ".join(notification.body_loc_args).lower())
                    self.assertEqual(notification.apns_custom_payload(), {
                        "flight_id": 42,
                        "update_type": "aircraft",
                        "previous_value": old_reg or old_model or "",
                        "new_value": new_reg or new_model or "",
                        "notification_id": notification.notification_id,
                    })
                    self.assertEqual(notification.priority, 50)

    def test_nonempty_models_preserve_every_existing_localized_branch(self):
        for new_model in ("Airbus A320", "  Airbus A320  "):
            branches = (
                ("assigned", None, None,
                 "Aircraft %@ (%@) has been assigned to your flight.", ["N100QA", new_model],
                 f"Aircraft N100QA ({new_model}) has been assigned to your flight."),
                ("registration_changed", "N099QA", "Boeing 737",
                 "Aircraft changed to %@ (%@).", ["N100QA", new_model],
                 f"Aircraft changed to N100QA ({new_model})."),
                ("model_changed", "N100QA", "Boeing 737",
                 "Aircraft %@ model updated to %@.", ["N100QA", new_model],
                 f"The aircraft model for your flight N100QA has been updated to {new_model}."),
                ("generic", "N100QA", new_model,
                 "Aircraft information has been updated for flight %@.", ["AA100"],
                 "Aircraft information has been updated for flight AA100."),
            )
            for branch, old_reg, old_model, loc_key, loc_args, legacy_body in branches:
                with self.subTest(branch=branch, new_model=new_model):
                    notification = ApnService.create_aircraft_updated_notification(
                        flight_id=42,
                        flight_number="AA100",
                        old_reg=old_reg,
                        new_reg="N100QA",
                        new_model=new_model,
                        old_model=old_model,
                    )

                    self.assertEqual(notification.body, legacy_body)
                    self.assertEqual(notification.body_loc_key, loc_key)
                    self.assertEqual(notification.body_loc_args, loc_args)
                    self.assertEqual(notification.previous_value, old_reg or old_model or "")
                    self.assertEqual(notification.new_value, "N100QA")


class NotificationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def email_notification(self):
        return ApnService.create_new_flight_added_notification(42, "AA100")

    def email_localized_alert(self):
        return {
            "title-loc-key": "New flight added to your account",
            "title-loc-args": [],
            "loc-key": "Flight %@ has been added to your account automatically from your forwarded email.",
            "loc-args": ["AA100"],
        }

    async def test_single_email_send_requires_explicit_v2_and_support(self):
        notification = self.email_notification()
        self.assertEqual(notification.title, "New flight added to your account")
        self.assertEqual(
            notification.body,
            'Flight "AA100" has been added to your account automatically from your forwarded email.',
        )
        cases = (
            ({}, False),
            ({"supports_localized_push": True}, False),
            ({"supports_localized_push": False, "localized_push_version": 2}, False),
            ({"supports_localized_push": True, "localized_push_version": 0}, False),
            ({"supports_localized_push": True, "localized_push_version": 1}, False),
            ({"supports_localized_push": True, "localized_push_version": 2}, True),
        )
        for options, localized in cases:
            with self.subTest(options=options):
                client = SimpleNamespace(send_notification=AsyncMock())
                with patch("core.services.apn.service.get_apns_client", return_value=client):
                    await ApnService.send_single_push_notification(
                        notification, "token-1", 7, **options,
                    )
                request = client.send_notification.call_args.args[0]
                self.assertEqual(request.message["aps"], {
                    "alert": self.email_localized_alert() if localized else {
                        "title": notification.title, "body": notification.body,
                    },
                    "badge": 7,
                })
                self.assertEqual(
                    {key: request.message[key] for key in notification.apns_custom_payload()},
                    notification.apns_custom_payload(),
                )
                self.assertNotIn("request_review", request.message)

    async def test_mixed_email_recipients_keep_independent_dictionary_capabilities(self):
        notification = self.email_notification()
        recipients = [
            device(localized=True, version=2, token="v2"),
            device(localized=True, version=1, token="v1"),
            device(localized=False, version=2, token="disabled"),
            device(localized=True, version=0, token="zero"),
        ]
        client = SimpleNamespace(send_notification=AsyncMock())
        with patch("core.services.apn.service.get_apns_client", return_value=client):
            await ApnService.send_multiple_push_notification(NotificationBatch(
                notification=notification, devices=recipients,
            ))
        requests = {call.args[0].device_token: call.args[0] for call in client.send_notification.call_args_list}
        self.assertEqual(set(requests), {"v2", "v1", "disabled", "zero"})
        for token, request in requests.items():
            self.assertEqual(request.message["aps"]["alert"],
                             self.email_localized_alert() if token == "v2" else {
                                 "title": notification.title, "body": notification.body,
                             })
            self.assertEqual(request.message["notification_id"], notification.notification_id)
            self.assertEqual(request.message["new_value"], "AA100")
            self.assertEqual(request.message["flight_id"], 42)

    def test_invalid_internal_versions_never_select_localized_keys(self):
        notifications = [self.email_notification(), ApnService.create_gate_change_notification(
            42, "Departure", "gate", None, "B12", "AA100",
        )]
        for notification in notifications:
            for version in (-1, 3, 99, True, "2", 2.0, None):
                with self.subTest(update_type=notification.update_type, version=version):
                    self.assertEqual(ApnService._build_alert(notification, True, version), {
                        "title": notification.title, "body": notification.body,
                    })

    def test_v1_notification_behavior_is_preserved_for_supported_versions(self):
        notification = ApnService.create_gate_change_notification(
            42, "Departure", "gate", "A1", "B12", "AA100",
        )
        for version in (1, 2):
            with self.subTest(version=version):
                self.assertEqual(ApnService._build_alert(notification, True, version), {
                    "title-loc-key": "Flight %@ details updated",
                    "title-loc-args": ["AA100"],
                    "loc-key": "Gate changed from %@ to %@ for flight %@.",
                    "loc-args": ["A1", "B12", "AA100"],
                })
        self.assertEqual(ApnService._build_alert(notification, True, 0), {
            "title": notification.title, "body": notification.body,
        })

    async def test_new_app_receives_localization_keys_and_review_flag(self):
        captured = []

        class FakeAPNsClient:
            async def send_notification(self, request):
                captured.append(request)

        notification = Notification(
            title="Flight AA100 details updated",
            body="Gate B12 is now available for flight AA100.",
            flight_id=42,
            update_type="gate",
            new_value="B12",
            title_loc_key="Flight %@ details updated",
            title_loc_args=["AA100"],
            body_loc_key="Gate %@ is now available for flight %@.",
            body_loc_args=["B12", "AA100"],
        )
        batch = NotificationBatch(
            notification=notification,
            devices=[device(localized=True)],
            invoke_review=True,
        )

        with patch(
            "core.services.apn.service.get_apns_client",
            return_value=FakeAPNsClient(),
        ):
            await ApnService.send_multiple_push_notification(batch)

        self.assertEqual(len(captured), 1)
        message = captured[0].message
        self.assertEqual(
            message["aps"]["alert"],
            {
                "title-loc-key": "Flight %@ details updated",
                "title-loc-args": ["AA100"],
                "loc-key": "Gate %@ is now available for flight %@.",
                "loc-args": ["B12", "AA100"],
            },
        )
        self.assertTrue(message["request_review"])

    async def test_released_app_keeps_english_fallback(self):
        captured = []

        class FakeAPNsClient:
            async def send_notification(self, request):
                captured.append(request)

        notification = Notification(
            title="Flight AA100 details updated",
            body="Gate B12 is now available for flight AA100.",
            flight_id=42,
            update_type="gate",
            title_loc_key="Flight %@ details updated",
            body_loc_key="Gate %@ is now available for flight %@.",
        )
        batch = NotificationBatch(
            notification=notification,
            devices=[device(localized=False)],
        )

        with patch(
            "core.services.apn.service.get_apns_client",
            return_value=FakeAPNsClient(),
        ):
            await ApnService.send_multiple_push_notification(batch)

        self.assertEqual(
            captured[0].message["aps"]["alert"],
            {
                "title": "Flight AA100 details updated",
                "body": "Gate B12 is now available for flight AA100.",
            },
        )


class LocalizedPushCapabilityTests(unittest.TestCase):
    def test_request_and_device_info_default_to_original_dictionary(self):
        request = users.RefreshApnToken(device_id="device-1", apn_token="token-1")
        self.assertEqual(request.localized_push_version, 1)
        self.assertFalse(request.supports_localized_push)
        self.assertEqual(device(localized=True).localized_push_version, 1)
        self.assertEqual(Device(id="device-1", user_id="user-1").localized_push_version, 1)
        for version in (0, 1, 2):
            with self.subTest(version=version):
                self.assertEqual(users.RefreshApnToken(
                    device_id="device-1", apn_token="token-1", localized_push_version=version,
                ).localized_push_version, version)
        for invalid in (-1, 3, True, "2", 2.0, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                device(localized=True, version=invalid)

    def test_invalid_or_unsupported_wire_versions_are_rejected_before_writes(self):
        app = FastAPI()
        app.include_router(users.router, prefix="/users")
        session = MagicMock(spec=Session)
        app.dependency_overrides[users.get_current_user] = lambda: User(id="user-1")
        app.dependency_overrides[users.get_session] = lambda: session
        with TestClient(app) as client:
            for version in (-1, 3, 99, True, "2", 2.0, None, [], {}):
                with self.subTest(version=version):
                    response = client.put("/users/me/apn/refresh", json={
                        "device_id": "device-1", "apn_token": "token-1",
                        "supports_localized_push": True, "localized_push_version": version,
                    })
                    self.assertEqual(response.status_code, 422)
        session.exec.assert_not_called()
        session.add.assert_not_called()
        session.commit.assert_not_called()

    def test_refresh_persists_version_and_missing_field_downgrades_safely(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.addCleanup(engine.dispose)
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            user = User(id="user-1")
            stored_device = Device(id="device-1", user_id=user.id)
            session.add(user)
            session.add(stored_device)
            session.commit()
            for options, expected in (({"localized_push_version": 2}, 2),
                                      ({}, 1), ({"localized_push_version": 0}, 0)):
                with self.subTest(options=options):
                    result = users.refresh_apn_token(users.RefreshApnToken(
                        device_id=stored_device.id, apn_token="token-1",
                        supports_localized_push=True, **options,
                    ), user, session)
                    session.refresh(stored_device)
                    self.assertEqual(result, {"detail": "APN token refreshed successfully"})
                    self.assertEqual(stored_device.localized_push_version, expected)
                    self.assertTrue(stored_device.supports_localized_push)
                    self.assertTrue(stored_device.apn_token_active)

    def test_flight_recipient_lookup_preserves_each_device_version(self):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        self.addCleanup(engine.dispose)
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            user = User(id="user-1", notification_count=4)
            flight = Flight(id=42, date="2026-09-12", number="AA100", status="Expected")
            user.flights.append(flight)
            for version in (0, 1, 2):
                user.devices.append(Device(
                    id=f"device-{version}", user_id=user.id,
                    apn_token=f"token-{version}", apn_token_active=True,
                    supports_localized_push=True, localized_push_version=version,
                ))
            session.add(user)
            session.commit()
            payload = ApnService.get_devices_payload_for_a_flight(42, session)
            self.assertEqual({item.token: item.localized_push_version for item in payload}, {
                "token-0": 0, "token-1": 1, "token-2": 2,
            })
            self.assertTrue(all(item.supports_localized_push and item.badge == 5 for item in payload))


class EmailPushCapabilityTests(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def email_worker_unit(self, *, sender="fixture@example.invalid", user_id="synthetic-user"):
        """Stub the ingress authority boundary, not its security contract.

        These notification/lifecycle units use structurally valid proof/object
        fixtures, but deliberately stub intake validation, byte verification,
        durable claiming, fresh consent and finalization. Real authentication,
        owner binding and replay behavior belong to test_email_ingress.py.
        """
        data = f"From: {sender}\r\nSubject: Synthetic fixture\r\n\r\nsynthetic email".encode()
        notification = S3EmailNotification(
            bucket="fixture", key="authenticated-v1/unit-message",
            receipt=SESReceiptProof(
                version=1, source="ses_direct", message_id="unit-message",
                received_at_ms=1, sender=sender, recipient="track@sofly.to",
                dmarc="PASS", spam="PASS", virus="PASS", headers_truncated=False,
                content_sha256=hashlib.sha256(data).hexdigest(),
                etag='"' + "a" * 32 + '"', version_id="unit-version",
            ),
        )
        payload = notification.model_dump(mode="json")
        context = SimpleNamespace(user_id=user_id)
        body = io.BytesIO(data)
        stream = MagicMock(wraps=body)
        s3_client = MagicMock()
        s3_client.get_object.return_value = {
            "Body": stream, "ContentLength": len(data),
            "ETag": notification.receipt.etag, "VersionId": notification.receipt.version_id,
        }
        with patch.object(background_tasks, "validate_intake", return_value=payload) as intake, \
             patch.object(background_tasks, "verify_object_bytes", return_value=sender) as verify, \
             patch.object(background_tasks, "claim_email_receipt", return_value=context) as claim, \
             patch.object(background_tasks, "require_email_consent_for_send") as consent, \
             patch.object(background_tasks, "finish_email_receipt") as finish, \
             patch.object(background_tasks, "get_s3_client", return_value=s3_client):
            yield SimpleNamespace(
                notification=notification, payload=payload, context=context, data=data,
                intake=intake, verify=verify, claim=claim, consent=consent, finish=finish,
            )
        intake.assert_called_once_with(notification)
        verify.assert_called_once_with(payload, data)
        claim.assert_called_once_with(notification)
        s3_client.get_object.assert_called_once_with(
            Bucket=notification.bucket, Key=notification.key,
            IfMatch=notification.receipt.etag, VersionId=notification.receipt.version_id,
        )
        stream.read.assert_called_once_with(background_tasks.MAX_EMAIL_BYTES + 1)
        stream.close.assert_called_once()
        self.assertTrue(body.closed)

    async def test_email_session_entry_and_cleanup_failures_are_bounded(self):
        private = "synthetic-private-sender@example.invalid private itinerary"
        for failed_phase in ("construction", "entry", "exit"):
            with self.subTest(failed_phase=failed_phase):
                session = MagicMock(spec=Session)
                session.__enter__.return_value = session
                if failed_phase == "entry":
                    session.__enter__.side_effect = RuntimeError(private)
                if failed_phase == "exit":
                    session.__exit__.side_effect = RuntimeError(private)
                constructor = MagicMock(return_value=session)
                if failed_phase == "construction":
                    constructor.side_effect = RuntimeError(private)
                # In the exit case, missing owner raises inside the entered
                # session; the injected cleanup exception must stay bounded.
                session.get.return_value = None
                parsed = EmailRead(sender="fixture@example.invalid", body=private)
                with self.email_worker_unit() as fixture, \
                     patch.object(background_tasks, "Session", constructor), \
                     patch.object(background_tasks, "parse_email", return_value=parsed) as parse, \
                     patch.object(background_tasks, "GeminiService") as gemini, \
                     self.assertLogs(background_tasks.logger, level="INFO") as captured:
                    await background_tasks.handle_incoming_email(fixture.notification)
                self.assertEqual([record.getMessage() for record in captured.records], ["forwarded_email_processing_failed"])
                for record in captured.records:
                    self.assertNotIn(private, repr(record.__dict__))
                    self.assertFalse(record.exc_info)
                    self.assertFalse(record.stack_info)
                constructor.assert_called_once_with(background_tasks.engine)
                parse.assert_called_once_with(fixture.data)
                gemini.assert_not_called()
                fixture.consent.assert_called_once_with(fixture.context)
                fixture.finish.assert_called_once_with(fixture.context, "failed")
                if failed_phase == "construction":
                    session.__enter__.assert_not_called()
                else:
                    session.__enter__.assert_called_once()
                if failed_phase == "exit":
                    session.get.assert_called_once_with(User, fixture.context.user_id)
                    session.__exit__.assert_called_once()
                    self.assertIs(session.__exit__.call_args.args[0], background_tasks.AIConsentRequired)
                else:
                    session.__exit__.assert_not_called()
                # The worker does not attempt an unsafe manual rollback after
                # context-manager construction/entry/cleanup has failed.
                session.rollback.assert_not_called()
                session.commit.assert_not_called()

    async def test_email_parser_and_finalization_failures_never_log_private_input(self):
        private = "synthetic-private-sender@example.invalid private itinerary https://example.invalid"
        for finalization_fails in (False, True):
            with self.subTest(finalization_fails=finalization_fails):
                session = MagicMock(spec=Session)
                session.__enter__.return_value = session
                with self.email_worker_unit() as fixture, \
                     patch.object(background_tasks, "Session", return_value=session) as constructor, \
                     patch.object(background_tasks, "parse_email", side_effect=ValueError(private)) as parse, \
                     patch.object(background_tasks, "GeminiService") as gemini, \
                     patch.object(ApnService, "send_single_push_notification", new_callable=AsyncMock) as send, \
                     self.assertLogs(background_tasks.logger, level="INFO") as captured:
                    if finalization_fails:
                        fixture.finish.side_effect = RuntimeError(private)
                    await background_tasks.handle_incoming_email(fixture.notification)
                expected = ["forwarded_email_processing_failed"]
                if finalization_fails:
                    expected.append("forwarded_email_receipt_finalize_failed")
                self.assertEqual([record.getMessage() for record in captured.records], expected)
                self.assertNotIn(private, " ".join(captured.output))
                self.assertNotIn("Traceback", " ".join(captured.output))
                for record in captured.records:
                    self.assertNotIn(private, repr(record.__dict__))
                    self.assertFalse(record.exc_info)
                    self.assertFalse(record.stack_info)
                parse.assert_called_once_with(fixture.data)
                constructor.assert_not_called()
                gemini.assert_not_called()
                send.assert_not_awaited()
                fixture.consent.assert_not_called()
                fixture.finish.assert_called_once_with(fixture.context, "failed")
                session.rollback.assert_not_called()
                session.commit.assert_not_called()

    async def test_unmatched_and_already_linked_email_paths_log_only_bounded_codes(self):
        private = "synthetic-private-key@example.invalid"
        for already_linked in (False, True):
            with self.subTest(already_linked=already_linked):
                session = MagicMock(spec=Session)
                session.__enter__.return_value = session
                user = User(id="synthetic-user", email=private, verified=True)
                session.get.return_value = user
                session.exec.return_value.first.return_value = object()
                parsed = EmailRead(sender=private, body="synthetic flight lookup")
                flight = Flight(id=42, date="2026-09-12", number="AA100", status="Expected")
                result = SimpleNamespace(handler=AsyncMock(return_value=[flight]), args={})
                ai_parser = SimpleNamespace(get_function_call=AsyncMock(return_value=result))
                original_badge = user.notification_count
                with self.email_worker_unit(sender=private, user_id=user.id) as fixture, \
                     patch.object(background_tasks, "Session", return_value=session) as constructor, \
                     patch.object(background_tasks, "parse_email", return_value=parsed) as parse, \
                     patch.object(background_tasks, "GeminiService", return_value=ai_parser) as gemini, \
                     patch.object(background_tasks.FlightPersistence, "link_flight_and_user") as link, \
                     patch.object(ApnService, "send_single_push_notification", new_callable=AsyncMock) as send, \
                     self.assertLogs(background_tasks.logger, level="INFO") as captured:
                    if not already_linked:
                        # Unmatched Apple owner is denied by the claim service;
                        # None would mean a duplicate, not an unmatched owner.
                        fixture.claim.side_effect = background_tasks.AIConsentRequired("forwarded_email")
                    await background_tasks.handle_incoming_email(fixture.notification)
                expected = "forwarded_email_flight_already_linked" if already_linked else "forwarded_email_ai_processing_not_authorized"
                self.assertEqual([record.getMessage() for record in captured.records], [expected])
                self.assertNotIn(private, " ".join(captured.output))
                self.assertTrue(all(not record.exc_info and not record.stack_info for record in captured.records))
                if already_linked:
                    constructor.assert_called_once_with(background_tasks.engine)
                    session.get.assert_called_once_with(User, fixture.context.user_id)
                    session.__enter__.assert_called_once()
                    session.__exit__.assert_called_once_with(None, None, None)
                    parse.assert_called_once_with(fixture.data)
                    gemini.assert_called_once_with(user_id=user.id, email_sender=private, email_receipt=fixture.context)
                    ai_parser.get_function_call.assert_awaited_once_with(query=parsed.body, email=True)
                    result.handler.assert_awaited_once_with(session=session)
                    self.assertEqual(fixture.consent.call_count, 3)
                    fixture.consent.assert_called_with(fixture.context)
                    fixture.finish.assert_called_once_with(fixture.context, "completed")
                else:
                    constructor.assert_not_called()
                    parse.assert_not_called()
                    gemini.assert_not_called()
                    fixture.consent.assert_not_called()
                    fixture.finish.assert_not_called()
                link.assert_not_called()
                send.assert_not_awaited()
                self.assertEqual(user.notification_count, original_badge)
                session.commit.assert_not_called()
                session.rollback.assert_not_called()

    async def test_email_import_preserves_capability_in_each_single_send(self):
        user = User(id="user-1", email="fixture@example.invalid", verified=True, notification_count=4)
        for version in (0, 1, 2):
            user.devices.append(Device(
                id=f"device-{version}", user_id=user.id, apn_token=f"token-{version}",
                apn_token_active=True, supports_localized_push=True, localized_push_version=version,
            ))
        user.devices.append(Device(
            id="disabled-support", user_id=user.id, apn_token="token-disabled",
            apn_token_active=True, supports_localized_push=False, localized_push_version=2,
        ))
        user.devices.append(Device(
            id="inactive", user_id=user.id, apn_token="inactive-token", apn_token_active=False,
        ))
        user.devices.append(Device(id="missing-token", user_id=user.id, apn_token_active=True))
        flight = Flight(id=42, date="2026-09-12", number="AA100", status="Expected")
        session = MagicMock(spec=Session)
        session.__enter__.return_value = session
        session.get.return_value = user
        session.exec.return_value.first.return_value = None
        parsed = EmailRead(sender=user.email, body="synthetic flight lookup")
        result = SimpleNamespace(handler=AsyncMock(return_value=[flight]), args={})
        ai_parser = SimpleNamespace(get_function_call=AsyncMock(return_value=result))
        send = AsyncMock()
        with self.email_worker_unit(sender=user.email, user_id=user.id) as fixture, \
                patch.object(background_tasks, "Session", return_value=session) as constructor, \
                patch.object(background_tasks, "parse_email", return_value=parsed) as parse, \
                patch.object(background_tasks, "GeminiService", return_value=ai_parser) as gemini, \
                patch.object(background_tasks.FlightPersistence, "link_flight_and_user") as link, \
                patch.object(ApnService, "send_single_push_notification", new=send):
            await background_tasks.handle_incoming_email(fixture.notification)
        calls = {call.kwargs["fcm_token"]: call.kwargs for call in send.call_args_list}
        self.assertEqual(send.await_count, 4)
        self.assertEqual(set(calls), {"token-0", "token-1", "token-2", "token-disabled"})
        for version in (0, 1, 2):
            self.assertEqual(calls[f"token-{version}"]["localized_push_version"], version)
            self.assertTrue(calls[f"token-{version}"]["supports_localized_push"])
        self.assertFalse(calls["token-disabled"]["supports_localized_push"])
        self.assertEqual(calls["token-disabled"]["localized_push_version"], 2)
        self.assertTrue(all(call["badge_count"] == 5 for call in calls.values()))
        self.assertEqual(len({call["notification"].notification_id for call in calls.values()}), 1)
        constructor.assert_called_once_with(background_tasks.engine)
        session.get.assert_called_once_with(User, user.id)
        session.__enter__.assert_called_once()
        session.__exit__.assert_called_once_with(None, None, None)
        parse.assert_called_once_with(fixture.data)
        gemini.assert_called_once_with(user_id=user.id, email_sender=user.email, email_receipt=fixture.context)
        ai_parser.get_function_call.assert_awaited_once_with(query=parsed.body, email=True)
        result.handler.assert_awaited_once_with(session=session)
        link.assert_called_once_with(session=session, flight_id=flight.id, user_id=user.id)
        self.assertEqual(fixture.consent.call_count, 3)
        fixture.consent.assert_called_with(fixture.context)
        fixture.finish.assert_called_once_with(fixture.context, "completed")
        self.assertEqual(user.notification_count, 5)
        session.commit.assert_called_once()
        session.rollback.assert_not_called()


class LocalizedPushMigrationTests(unittest.TestCase):
    def test_migration_preserves_old_capabilities_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="sofly-push-migration-") as scratch:
            path = Path(scratch) / "fixture.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE device (id TEXT PRIMARY KEY, supports_localized_push BOOLEAN NOT NULL DEFAULT 0)")
                connection.executemany("INSERT INTO device VALUES (?, ?)", [("old", 0), ("localized", 1)])
            self.assertTrue(migrate(path))
            self.assertFalse(migrate(path))
            with closing(sqlite3.connect(path)) as connection, connection:
                self.assertEqual(connection.execute(
                    "SELECT id, supports_localized_push, localized_push_version FROM device ORDER BY id"
                ).fetchall(), [("localized", 1, 1), ("old", 0, 1)])
                for invalid in (-1, 3, 2.5, None):
                    with self.subTest(invalid=invalid), self.assertRaises(sqlite3.IntegrityError):
                        connection.execute("UPDATE device SET localized_push_version = ?", (invalid,))

    def test_migration_requires_existing_database_and_prior_capability(self):
        with tempfile.TemporaryDirectory(prefix="sofly-push-migration-") as scratch:
            path = Path(scratch) / "fixture.db"
            with self.assertRaises(FileNotFoundError):
                migrate(path)
            self.assertFalse(path.exists())
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE device (id TEXT PRIMARY KEY)")
            with self.assertRaises(RuntimeError):
                migrate(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                self.assertEqual([row[1] for row in connection.execute("PRAGMA table_info(device)")], ["id"])


if __name__ == "__main__":
    unittest.main()
