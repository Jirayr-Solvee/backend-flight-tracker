"""Synthetic credential-bearing APNs failures must not become log payloads."""

import unittest
from datetime import datetime, timezone
from unittest import mock

from tests import test_live_activity as fixtures

from aioapns.common import NotificationResult
from sqlalchemy.exc import StatementError
from sqlmodel import Session, SQLModel, create_engine

from core.models.device import Device
from core.models.live_activity import (
    LiveActivityPushToStartDelivery, LiveActivityPushToStartRegistration,
)
from core.models.user import User, UserFlightLink
from core.services.apn.live_activity import LiveActivityService


class LiveActivityCredentialLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_exception_and_chain_are_omitted_without_changing_retry(self):
        test_engine = create_engine("sqlite://")
        self.addCleanup(test_engine.dispose)
        SQLModel.metadata.create_all(test_engine)
        push_token = "a1" * 32
        signed_credential = "synthetic.authorization.must.not.be.logged"
        with Session(test_engine) as session:
            user = User(id="credential-log-user")
            device = Device(id="credential-log-device", user_id=user.id)
            flight = fixtures.LiveActivityPayloadTests._flight(status="Expected")
            session.add(user)
            session.add(device)
            session.add(flight)
            session.commit()
            flight_id = flight.id
            session.add(UserFlightLink(user_id=user.id, flight_id=flight_id))
            session.add(LiveActivityPushToStartRegistration(
                device_id=device.id, push_token=push_token, apns_environment="sandbox",
            ))
            session.commit()

        class FailingThenSuccessfulAPNs:
            def __init__(self):
                self.requests = []

            async def send_notification(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    try:
                        raise RuntimeError(signed_credential)
                    except RuntimeError as cause:
                        raise StatementError(
                            "synthetic provider failure",
                            "INSERT INTO sensitive_delivery (push_token) VALUES (?)",
                            {"push_token": push_token}, cause,
                        ) from cause
                return NotificationResult(notification_id=request.notification_id, status="200")

        client = FailingThenSuccessfulAPNs()
        now = datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)
        with mock.patch("core.services.apn.live_activity.engine", test_engine), \
                mock.patch("core.services.apn.live_activity.get_apns_client", return_value=client):
            with self.assertLogs("core.services.apn.live_activity", level="ERROR") as captured:
                await LiveActivityService.start_due_activities(
                    device_id="credential-log-device", now=now,
                )
            self.assertEqual(len(captured.records), 1)
            record = captured.records[0]
            self.assertEqual(record.getMessage(),
                             "live_activity_push_to_start_failed reason=delivery_exception")
            self.assertFalse(record.exc_info)
            self.assertFalse(record.stack_info)
            emitted = "\n".join(captured.output)
            for sensitive in (push_token, signed_credential, "INSERT INTO", "Traceback"):
                self.assertNotIn(sensitive, emitted)

            with Session(test_engine) as session:
                delivery = session.get(LiveActivityPushToStartDelivery,
                                       ("credential-log-device", flight_id))
                self.assertEqual(delivery.state, "failed_transient")
                self.assertEqual(delivery.attempt_count, 1)
                self.assertEqual(delivery.last_apns_status, "exception")
                self.assertEqual(delivery.last_apns_reason, "StatementError")
                self.assertIsNone(delivery.delivered_at)
                registration = session.get(LiveActivityPushToStartRegistration,
                                           "credential-log-device")
                self.assertTrue(registration.active)
                self.assertIsNone(registration.last_started_flight_id)

            await LiveActivityService.start_due_activities(
                device_id="credential-log-device", now=now,
            )
            self.assertEqual(len(client.requests), 2)
            with Session(test_engine) as session:
                delivery = session.get(LiveActivityPushToStartDelivery,
                                       ("credential-log-device", flight_id))
                self.assertEqual(delivery.state, "delivered")
                self.assertEqual(delivery.attempt_count, 2)
                self.assertIsNotNone(delivery.delivered_at)
