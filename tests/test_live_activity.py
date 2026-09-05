import os
import asyncio
import json
import tempfile
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
from core.models.live_activity import (
    LiveActivityPushToStartDelivery,
    LiveActivityPushToStartRegistration,
    LiveActivityRegistration,
)
from core.models.user import User, UserFlightLink
from core.background_tasks import create_webhook_for_flight
from core.routers.users import (
    RegisterLiveActivityPushToStartRequest,
    RegisterLiveActivityRequest,
    register_live_activity,
    register_live_activity_push_to_start,
    unregister_live_activity,
)
from core.services.apn.live_activity import (
    LiveActivityService,
    make_live_activity_payload,
    make_push_to_start_payload,
)


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
                "operationalDetailKind",
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
        self.assertEqual(state["operationalDetailKind"], "gate")
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

    def test_push_to_start_payload_contains_attributes_and_initial_state(self):
        now = datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)

        payload = make_push_to_start_payload(self._flight(), now=now)
        aps = payload["aps"]

        self.assertEqual(aps["event"], "start")
        self.assertEqual(aps["attributes-type"], "mainWidgetAttributes")
        self.assertEqual(aps["attributes"], {"flightId": 42})
        self.assertEqual(aps["content-state"]["flightId"], 42)
        self.assertEqual(
            aps["alert"]["title-loc-key"],
            "Live tracking started for %@",
        )

    def test_predeparture_stages_show_departure_gate(self):
        for status in ("Expected", "CheckIn", "Boarding", "GateClosed", "Delayed"):
            with self.subTest(status=status):
                state = make_live_activity_payload(self._flight(status))[0]["aps"]["content-state"]
                self.assertEqual(state["gateLabel"], "31B")
                self.assertEqual(state["operationalDetailKind"], "gate")
        for status in ("EnRoute", "Departed", "Approaching", "Arrived"):
            with self.subTest(status=status):
                state = make_live_activity_payload(self._flight(status))[0]["aps"]["content-state"]
                self.assertEqual(state["gateLabel"], "B14")

    def test_operational_fallback_reports_actual_kind_and_skips_placeholders(self):
        flight = self._flight("Boarding")
        flight.departure.gate = " Unknown "
        flight.departure.terminal = " 2 "
        flight.departure.checkin_desk = "17-22"
        flight.departure.baggage_belt = "4"
        state = make_live_activity_payload(flight)[0]["aps"]["content-state"]
        self.assertEqual((state["operationalDetailKind"], state["gateLabel"]), ("terminal", "2"))
        flight.departure.terminal = "—"
        state = make_live_activity_payload(flight)[0]["aps"]["content-state"]
        self.assertEqual((state["operationalDetailKind"], state["gateLabel"]), ("check_in", "17-22"))
        flight.departure.checkin_desk = "none"
        state = make_live_activity_payload(flight)[0]["aps"]["content-state"]
        self.assertEqual((state["operationalDetailKind"], state["gateLabel"]), ("baggage", "4"))
        flight.departure.baggage_belt = "n/a"
        state = make_live_activity_payload(flight)[0]["aps"]["content-state"]
        self.assertIsNone(state["operationalDetailKind"])
        self.assertIsNone(state["gateLabel"])


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
            session.add(
                LiveActivityPushToStartDelivery(
                    device_id=device.id,
                    flight_id=flight.id,
                    push_token_fingerprint="fingerprint",
                    state="delivered",
                )
            )
            session.commit()

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
            push_start_delivery = session.get(
                LiveActivityPushToStartDelivery,
                (device.id, flight.id),
            )
            self.assertEqual(push_start_delivery.state, "confirmed")
            self.assertIsNotNone(push_start_delivery.confirmed_at)

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

    def test_push_to_start_registration_is_scoped_to_the_users_device(self):
        with Session(self.engine) as session:
            user, device, _ = self._seed_registration_owner(session)

            response = register_live_activity_push_to_start(
                data=RegisterLiveActivityPushToStartRequest(
                    device_id=device.id,
                    push_token="ab" * 32,
                    apns_environment="sandbox",
                    uses_12_hour_time=True,
                ),
                user=user,
                session=session,
            )

            self.assertEqual(
                response,
                {"detail": "Live Activity push-to-start token registered"},
            )
            registration = session.get(
                LiveActivityPushToStartRegistration, device.id
            )
            self.assertIsNotNone(registration)
            self.assertEqual(registration.push_token, "ab" * 32)
            self.assertEqual(registration.apns_environment, "sandbox")
            self.assertTrue(registration.uses_12_hour_time)

            registration.last_started_flight_id = 42
            registration.last_start_at = 123
            session.add(registration)
            session.commit()
            register_live_activity_push_to_start(
                data=RegisterLiveActivityPushToStartRequest(
                    device_id=device.id,
                    push_token="cd" * 32,
                    apns_environment="sandbox",
                    uses_12_hour_time=True,
                ),
                user=user,
                session=session,
            )
            session.refresh(registration)
            self.assertEqual(registration.push_token, "cd" * 32)
            self.assertIsNone(registration.last_started_flight_id)
            self.assertIsNone(registration.last_start_at)


class LiveActivityDeliveryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _seed_activity(test_engine):
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            user = User(id="concurrency-user")
            device = Device(id="concurrency-device", user_id=user.id)
            flight = LiveActivityPayloadTests._flight("Boarding")
            session.add_all([user, device, flight])
            session.commit()
            session.add(LiveActivityRegistration(
                activity_id="concurrency-activity", push_token="ab" * 32,
                flight_id=flight.id, device_id=device.id,
            ))
            session.commit()
            return flight.id

    async def test_overlapping_workers_reserve_unique_timestamps_and_old_completion_cannot_rewind(self):
        with tempfile.TemporaryDirectory(prefix="sofly-live-activity-test-") as directory:
            test_engine = create_engine(f"sqlite:///{directory}/events.sqlite")
            flight_id = self._seed_activity(test_engine)
            first_in_flight, release_first = asyncio.Event(), asyncio.Event()

            class OverlappingAPNsClient:
                def __init__(self):
                    self.requests = []

                async def send_notification(self, request):
                    self.requests.append(request)
                    if len(self.requests) == 1:
                        first_in_flight.set()
                        await release_first.wait()
                    return NotificationResult(notification_id=request.notification_id, status="200")

            client = OverlappingAPNsClient()
            with mock.patch("core.services.apn.live_activity.engine", test_engine), \
                    mock.patch("core.services.apn.live_activity.get_apns_client", return_value=client), \
                    mock.patch("core.services.apn.live_activity.time.time", return_value=1_788_000_000):
                first = asyncio.create_task(LiveActivityService.send_updates_for_flight(flight_id))
                await asyncio.wait_for(first_in_flight.wait(), timeout=2)
                with Session(test_engine) as session:
                    flight = session.get(Flight, flight_id)
                    flight.departure.gate = "99"
                    session.add(flight.departure)
                    session.commit()
                await LiveActivityService.send_updates_for_flight(flight_id)
                release_first.set()
                await first
                await LiveActivityService.send_updates_for_flight(flight_id)

            self.assertEqual(len(client.requests), 2)
            timestamps = [request.message["aps"]["timestamp"] for request in client.requests]
            self.assertEqual(timestamps, [1_788_000_000, 1_788_000_001])
            with Session(test_engine) as session:
                row = session.get(LiveActivityRegistration, "concurrency-activity")
                self.assertEqual(row.last_apns_timestamp, timestamps[1])
                self.assertEqual(json.loads(row.last_content_state_json)["gateLabel"], "99")
            test_engine.dispose()

    async def test_failed_reserved_update_is_retried_without_suppressing_unchanged_content(self):
        test_engine = create_engine("sqlite://")
        flight_id = self._seed_activity(test_engine)

        class TransientAPNsClient:
            def __init__(self):
                self.requests = []

            async def send_notification(self, request):
                self.requests.append(request)
                return NotificationResult(notification_id=request.notification_id,
                    status="503" if len(self.requests) <= 3 else "200")

        client = TransientAPNsClient()
        with mock.patch("core.services.apn.live_activity.engine", test_engine), \
                mock.patch("core.services.apn.live_activity.get_apns_client", return_value=client), \
                mock.patch("core.services.apn.live_activity.time.time", return_value=1_788_000_000), \
                mock.patch("core.services.apn.live_activity.asyncio.sleep", new_callable=mock.AsyncMock):
            await LiveActivityService.send_updates_for_flight(flight_id)
            with Session(test_engine) as session:
                row = session.get(LiveActivityRegistration, "concurrency-activity")
                self.assertIsNone(row.last_content_state_json)
                self.assertTrue(row.active)
            await LiveActivityService.send_updates_for_flight(flight_id)
            await LiveActivityService.send_updates_for_flight(flight_id)
        self.assertEqual(len(client.requests), 4)
        self.assertEqual([request.message["aps"]["timestamp"] for request in client.requests],
                         [1_788_000_000, 1_788_000_000, 1_788_000_000, 1_788_000_001])
        test_engine.dispose()

    async def test_newer_failure_and_older_success_cannot_suppress_correction_to_previous_value(self):
        with tempfile.TemporaryDirectory(prefix="sofly-live-activity-test-") as directory:
            test_engine = create_engine(f"sqlite:///{directory}/events.sqlite")
            flight_id = self._seed_activity(test_engine)
            older_in_flight, release_older = asyncio.Event(), asyncio.Event()

            def set_gate(value):
                with Session(test_engine) as session:
                    flight = session.get(Flight, flight_id)
                    flight.departure.gate = value
                    session.add(flight.departure)
                    session.commit()

            class ReorderedAPNsClient:
                def __init__(self):
                    self.gates = []

                async def send_notification(self, request):
                    gate = request.message["aps"]["content-state"]["gateLabel"]
                    self.gates.append(gate)
                    if gate == "B":
                        older_in_flight.set()
                        await release_older.wait()
                    return NotificationResult(notification_id=request.notification_id,
                        status="503" if gate == "C" else "200")

            client = ReorderedAPNsClient()
            with mock.patch("core.services.apn.live_activity.engine", test_engine), \
                    mock.patch("core.services.apn.live_activity.get_apns_client", return_value=client), \
                    mock.patch("core.services.apn.live_activity.asyncio.sleep", new_callable=mock.AsyncMock):
                set_gate("A")
                await LiveActivityService.send_updates_for_flight(flight_id)
                set_gate("B")
                older = asyncio.create_task(LiveActivityService.send_updates_for_flight(flight_id))
                await asyncio.wait_for(older_in_flight.wait(), timeout=2)
                set_gate("C")
                await LiveActivityService.send_updates_for_flight(flight_id)
                release_older.set()
                await older
                with Session(test_engine) as session:
                    row = session.get(LiveActivityRegistration, "concurrency-activity")
                    self.assertIsNone(row.last_content_state_json)
                set_gate("A")
                await LiveActivityService.send_updates_for_flight(flight_id)
            self.assertEqual(client.gates, ["A", "B", "C", "C", "C", "A"])
            with Session(test_engine) as session:
                row = session.get(LiveActivityRegistration, "concurrency-activity")
                self.assertEqual(json.loads(row.last_content_state_json)["gateLabel"], "A")
            test_engine.dispose()

    async def test_due_flight_is_started_once_with_push_to_start_token(self):
        test_engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(test_engine)

        with Session(test_engine) as session:
            user = User(id="user-start")
            device = Device(id="device-start", user_id=user.id)
            flight = LiveActivityPayloadTests._flight(status="Expected")
            session.add(user)
            session.add(device)
            session.add(flight)
            session.commit()
            session.refresh(flight)
            session.add(UserFlightLink(user_id=user.id, flight_id=flight.id))
            session.add(
                LiveActivityPushToStartRegistration(
                    device_id=device.id,
                    push_token="ab" * 32,
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
        now = datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)
        with mock.patch(
            "core.services.apn.live_activity.engine", test_engine
        ), mock.patch(
            "core.services.apn.live_activity.get_apns_client",
            return_value=client,
        ) as client_factory:
            await LiveActivityService.start_due_activities(
                device_id="device-start",
                now=now,
            )
            await LiveActivityService.start_due_activities(
                device_id="device-start",
                now=now,
            )

        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertEqual(request.push_type, PushType.LIVEACTIVITY)
        self.assertEqual(request.message["aps"]["event"], "start")
        self.assertEqual(request.message["aps"]["attributes"]["flightId"], flight_id)
        client_factory.assert_called_with(use_sandbox=True)

        with Session(test_engine) as session:
            registration = session.get(
                LiveActivityPushToStartRegistration, "device-start"
            )
            self.assertEqual(registration.last_started_flight_id, flight_id)
            self.assertEqual(registration.last_apns_status, "200")
            delivery = session.get(
                LiveActivityPushToStartDelivery,
                ("device-start", flight_id),
            )
            self.assertEqual(delivery.state, "delivered")
            self.assertEqual(delivery.attempt_count, 1)
            self.assertIsNotNone(delivery.delivered_at)

    async def test_overlapping_flights_are_deduplicated_per_device_and_flight(self):
        test_engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(test_engine)

        with Session(test_engine) as session:
            user = User(id="user-overlap")
            device = Device(id="device-overlap", user_id=user.id)
            first = LiveActivityPayloadTests._flight(status="Expected")
            second = LiveActivityPayloadTests._flight(status="Expected")
            first.id = 42
            second.id = 43
            second.number = "AA101"
            session.add(user)
            session.add(device)
            session.add(first)
            session.add(second)
            session.commit()
            session.add(UserFlightLink(user_id=user.id, flight_id=first.id))
            session.add(UserFlightLink(user_id=user.id, flight_id=second.id))
            session.add(
                LiveActivityPushToStartRegistration(
                    device_id=device.id,
                    push_token="ab" * 32,
                    apns_environment="sandbox",
                )
            )
            session.commit()

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
        now = datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)
        with mock.patch(
            "core.services.apn.live_activity.engine", test_engine
        ), mock.patch(
            "core.services.apn.live_activity.get_apns_client",
            return_value=client,
        ):
            await LiveActivityService.start_due_activities(
                device_id="device-overlap",
                now=now,
            )
            await LiveActivityService.start_due_activities(
                device_id="device-overlap",
                now=now,
            )

        self.assertEqual(len(client.requests), 2)
        started_flight_ids = {
            request.message["aps"]["attributes"]["flightId"]
            for request in client.requests
        }
        self.assertEqual(started_flight_ids, {42, 43})

        with Session(test_engine) as session:
            deliveries = session.exec(
                select(LiveActivityPushToStartDelivery)
            ).all()
            self.assertEqual(len(deliveries), 2)
            self.assertEqual({item.state for item in deliveries}, {"delivered"})

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
