import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from aioapns import PushType
from aioapns.common import NotificationResult
from fastapi import BackgroundTasks, HTTPException
from sqlmodel import Session, SQLModel, create_engine, select


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


from core.models.device import Device
from core.models.flight import Airline, Airport, Arrival, Departure, Flight
from core.models.live_activity import LiveActivityRegistration
from core.models.user import User, UserFlightLink
from core.background_tasks import create_webhook_for_flight
from core.routers.users import (
    RegisterLiveActivityRequest,
    register_live_activity,
    unregister_live_activity,
)
from core.services.apn.live_activity import LiveActivityService, make_live_activity_payload


class LiveActivityPayloadTests(unittest.TestCase):
    @staticmethod
    def _flight(status: str = "EnRoute") -> Flight:
        departure = Departure(
            airport=Airport(
                iata="LAX",
                name="Los Angeles International",
                municipality_name="Los Angeles",
                lat=33.94,
                lon=-118.40,
                country_code="US",
            ),
            scheduled_time_local="2026-08-03T09:00:00-07:00",
            scheduled_time_utc="2026-08-03T16:00:00Z",
            revised_time_local="2026-08-03T09:25:00-07:00",
            revised_time_utc="2026-08-03T16:25:00Z",
            gate="31B",
        )
        arrival = Arrival(
            airport=Airport(
                iata="JFK",
                name="John F. Kennedy International",
                municipality_name="New York",
                lat=40.64,
                lon=-73.78,
                country_code="US",
            ),
            scheduled_time_local="2026-08-03T17:30:00-04:00",
            scheduled_time_utc="2026-08-03T21:30:00Z",
            predicted_time_local="2026-08-03T17:55:00-04:00",
            predicted_time_utc="2026-08-03T21:55:00Z",
            gate="B14",
        )
        return Flight(
            id=42,
            date="2026-08-03",
            number="AA100",
            status=status,
            airline=Airline(
                name="American Airlines",
                iata="AA",
                icao="AAL",
            ),
            departure=departure,
            arrival=arrival,
        )

    def test_update_payload_matches_widget_content_state_contract(self):
        now = datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc)

        payload, event, _ = make_live_activity_payload(self._flight(), now=now)
        aps = payload["aps"]
        state = aps["content-state"]

        self.assertEqual(event, "update")
        self.assertEqual(aps["timestamp"], 1_785_780_000)
        self.assertEqual(aps["stale-date"], 1_785_797_700)
        self.assertNotIn("dismissal-date", aps)
        self.assertEqual(
            set(state),
            {
                "flightId",
                "flightNumber",
                "airlineName",
                "airlineIata",
                "departureIata",
                "arrivalIata",
                "departureTimeLabel",
                "arrivalTimeLabel",
                "statusLabel",
                "gateLabel",
                "progress",
                "departureDate",
                "arrivalDate",
                "isDelayed",
            },
        )
        self.assertEqual(state["flightId"], 42)
        self.assertEqual(state["flightNumber"], "AA100")
        self.assertEqual(state["departureIata"], "LAX")
        self.assertEqual(state["arrivalIata"], "JFK")
        self.assertEqual(state["departureTimeLabel"], "09:25")
        self.assertEqual(state["arrivalTimeLabel"], "17:55")
        self.assertEqual(state["statusLabel"], "En Route")
        self.assertEqual(state["gateLabel"], "B14")
        self.assertTrue(state["isDelayed"])

        # Swift's default Codable Date strategy uses seconds since 2001,
        # unlike the Unix seconds required by the top-level APNs fields.
        self.assertEqual(state["departureDate"], 807_467_100.0)
        self.assertEqual(state["arrivalDate"], 807_486_900.0)

    def test_terminal_status_ends_activity_and_uses_twelve_hour_labels(self):
        now = datetime(2026, 8, 3, 22, 0, tzinfo=timezone.utc)

        payload, event, _ = make_live_activity_payload(
            self._flight(status="Arrived"),
            uses_12_hour_time=True,
            now=now,
        )
        aps = payload["aps"]
        state = aps["content-state"]

        self.assertEqual(event, "end")
        self.assertEqual(aps["timestamp"], 1_785_794_400)
        self.assertEqual(aps["dismissal-date"], 1_785_808_800)
        self.assertNotIn("stale-date", aps)
        self.assertEqual(state["statusLabel"], "Arrived")
        self.assertEqual(state["progress"], 1.0)
        self.assertEqual(state["departureTimeLabel"], "9:25 AM")
        self.assertEqual(state["arrivalTimeLabel"], "5:55 PM")


class LiveActivityRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def _seed_registration_owner(self, session: Session):
        user = User(id="user-1")
        device = Device(id="device-1", user_id=user.id)
        flight = Flight(
            date="2026-08-03",
            number="AA100",
            status="EnRoute",
        )
        session.add(user)
        session.add(device)
        session.add(flight)
        session.commit()
        session.refresh(flight)
        session.add(UserFlightLink(user_id=user.id, flight_id=flight.id))
        session.commit()
        return user, device, flight

    def test_registration_rotates_token_and_unregisters(self):
        with Session(self.engine) as session:
            user, device, flight = self._seed_registration_owner(session)
            background_tasks = BackgroundTasks()

            response = register_live_activity(
                activity_id="activity-1",
                data=RegisterLiveActivityRequest(
                    device_id=device.id,
                    flight_id=flight.id,
                    push_token="ab" * 32,
                    apns_environment="sandbox",
                    uses_12_hour_time=True,
                ),
                background_tasks=background_tasks,
                user=user,
                session=session,
            )
            self.assertEqual(response, {"detail": "Live Activity registered"})
            self.assertEqual(len(background_tasks.tasks), 2)
            self.assertIs(
                background_tasks.tasks[0].func,
                create_webhook_for_flight,
            )
            self.assertEqual(
                background_tasks.tasks[0].args,
                (flight.number, flight.id),
            )

            register_live_activity(
                activity_id="activity-1",
                data=RegisterLiveActivityRequest(
                    device_id=device.id,
                    flight_id=flight.id,
                    push_token="cd" * 32,
                    apns_environment="sandbox",
                    uses_12_hour_time=False,
                ),
                background_tasks=BackgroundTasks(),
                user=user,
                session=session,
            )

            registration = session.get(LiveActivityRegistration, "activity-1")
            self.assertIsNotNone(registration)
            self.assertEqual(registration.push_token, "cd" * 32)
            self.assertFalse(registration.uses_12_hour_time)
            self.assertTrue(registration.active)

            unregister_live_activity(
                activity_id="activity-1",
                device_id=device.id,
                user=user,
                session=session,
            )
            session.refresh(registration)
            self.assertFalse(registration.active)

            registrations = session.exec(select(LiveActivityRegistration)).all()
            self.assertEqual(len(registrations), 1)

    def test_registration_rejects_activity_owned_by_another_device(self):
        with Session(self.engine) as session:
            user, device, flight = self._seed_registration_owner(session)
            register_live_activity(
                activity_id="activity-1",
                data=RegisterLiveActivityRequest(
                    device_id=device.id,
                    flight_id=flight.id,
                    push_token="ab" * 32,
                ),
                background_tasks=BackgroundTasks(),
                user=user,
                session=session,
            )

            other_user = User(id="user-2")
            other_device = Device(id="device-2", user_id=other_user.id)
            session.add(other_user)
            session.add(other_device)
            session.add(UserFlightLink(user_id=other_user.id, flight_id=flight.id))
            session.commit()

            with self.assertRaises(HTTPException) as raised:
                register_live_activity(
                    activity_id="activity-1",
                    data=RegisterLiveActivityRequest(
                        device_id=other_device.id,
                        flight_id=flight.id,
                        push_token="ef" * 32,
                    ),
                    background_tasks=BackgroundTasks(),
                    user=other_user,
                    session=session,
                )

            self.assertEqual(raised.exception.status_code, 409)


class LiveActivityDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_uses_liveactivity_topic_deduplicates_and_ends(self):
        test_engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(test_engine)

        with Session(test_engine) as session:
            user = User(id="user-1")
            device = Device(id="device-1", user_id=user.id)
            flight = LiveActivityPayloadTests._flight()
            session.add(user)
            session.add(device)
            session.add(flight)
            session.commit()
            session.refresh(flight)
            session.add(UserFlightLink(user_id=user.id, flight_id=flight.id))
            session.add(
                LiveActivityRegistration(
                    activity_id="activity-1",
                    push_token="ab" * 32,
                    flight_id=flight.id,
                    device_id=device.id,
                    apns_environment="sandbox",
                )
            )
            session.commit()
            flight_id = flight.id

        class FakeAPNsClient:
            def __init__(self):
                self.requests = []

            async def send_notification(self, request):
                self.requests.append(request)
                return NotificationResult(
                    notification_id=request.notification_id,
                    status="200",
                )

        client = FakeAPNsClient()
        with mock.patch(
            "core.services.apn.live_activity.engine", test_engine
        ), mock.patch(
            "core.services.apn.live_activity.get_apns_client",
            return_value=client,
        ) as client_factory:
            await LiveActivityService.send_updates_for_flight(flight_id)
            self.assertEqual(len(client.requests), 1)
            self.assertEqual(client.requests[0].push_type, PushType.LIVEACTIVITY)
            self.assertEqual(
                client.requests[0].apns_topic,
                "com.zhirayr.Flight-tracker-shared.push-type.liveactivity",
            )
            self.assertEqual(client.requests[0].message["aps"]["event"], "update")
            client_factory.assert_called_with(use_sandbox=True)

            # An identical webhook snapshot should not consume another
            # ActivityKit push update.
            await LiveActivityService.send_updates_for_flight(flight_id)
            self.assertEqual(len(client.requests), 1)

            with Session(test_engine) as session:
                flight = session.get(Flight, flight_id)
                flight.status = "Arrived"
                session.add(flight)
                session.commit()

            await LiveActivityService.send_updates_for_flight(flight_id)
            self.assertEqual(len(client.requests), 2)
            self.assertEqual(client.requests[1].message["aps"]["event"], "end")

        with Session(test_engine) as session:
            registration = session.get(LiveActivityRegistration, "activity-1")
            self.assertFalse(registration.active)
            self.assertEqual(registration.last_apns_status, "200")
            self.assertIsNotNone(registration.last_apns_timestamp)

    async def test_expired_activity_token_is_deactivated(self):
        test_engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(test_engine)

        with Session(test_engine) as session:
            user = User(id="user-1")
            device = Device(id="device-1", user_id=user.id)
            flight = LiveActivityPayloadTests._flight(status="Arrived")
            session.add(user)
            session.add(device)
            session.add(flight)
            session.commit()
            session.refresh(flight)
            session.add(UserFlightLink(user_id=user.id, flight_id=flight.id))
            session.add(
                LiveActivityRegistration(
                    activity_id="activity-expired",
                    push_token="ab" * 32,
                    flight_id=flight.id,
                    device_id=device.id,
                    apns_environment="production",
                )
            )
            session.commit()
            flight_id = flight.id

        class ExpiredAPNsClient:
            async def send_notification(self, request):
                return NotificationResult(
                    notification_id=request.notification_id,
                    status="410",
                    description="ExpiredToken",
                )

        with mock.patch(
            "core.services.apn.live_activity.engine", test_engine
        ), mock.patch(
            "core.services.apn.live_activity.get_apns_client",
            return_value=ExpiredAPNsClient(),
        ):
            await LiveActivityService.send_updates_for_flight(flight_id)

        with Session(test_engine) as session:
            registration = session.get(
                LiveActivityRegistration,
                "activity-expired",
            )
            self.assertFalse(registration.active)
            self.assertEqual(registration.last_apns_status, "410")
            self.assertEqual(registration.last_apns_reason, "ExpiredToken")


class LiveActivityWebhookMonitoringTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_subscription_is_attached_to_requested_flight_id(self):
        test_engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(test_engine)

        with Session(test_engine) as session:
            stale_flight = Flight(
                date="2026-08-01",
                number="AA100",
                status="Arrived",
            )
            requested_flight = Flight(
                date="2026-08-03",
                number="AA100",
                status="Expected",
            )
            monitored_flight = Flight(
                date="2026-08-02",
                number="AA100",
                status="EnRoute",
                subscription_id="subscription-1",
                has_subscribed=True,
            )
            session.add(stale_flight)
            session.add(requested_flight)
            session.add(monitored_flight)
            session.commit()
            session.refresh(stale_flight)
            session.refresh(requested_flight)
            requested_id = requested_flight.id
            stale_id = stale_flight.id

        with mock.patch("core.background_tasks.engine", test_engine):
            await create_webhook_for_flight("AA100", requested_id)

        with Session(test_engine) as session:
            requested_flight = session.get(Flight, requested_id)
            stale_flight = session.get(Flight, stale_id)
            self.assertEqual(
                requested_flight.subscription_id,
                "subscription-1",
            )
            self.assertTrue(requested_flight.has_subscribed)
            self.assertIsNone(stale_flight.subscription_id)
            self.assertFalse(stale_flight.has_subscribed)


if __name__ == "__main__":
    unittest.main()
