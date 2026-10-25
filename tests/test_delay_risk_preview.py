"""Onboarding delay preview (POST /flights/delay-preview); scratch SQLite and a mocked fetcher only."""

import asyncio
import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from tests import test_experiment_reporting as _settings_fixture  # noqa: F401  (harmless settings first)

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine, select

from core import closable_flow_policy as policy
from core.config import Settings, settings
from core.models import get_session
from core.models.delay_preview import DelayPreviewBudget, DelayPreviewCache, DelayPreviewClaim
from core.models.flight import Airport, Arrival, Departure, Flight
from core.models.subscription import Subscription
from core.models.user import User, UserFlightLink, UserSubscriptionLink
from core.routers import activation_journey, flights, users
from core.routers.experiment_diagnostics import DiagnosticEvent
from core.services.flight import api_client, delay_preview
from core.services.flight.delay_preview import flight_identity, primary_reason, request_delay_preview
from core.services.flight.delay_risk import DelayRiskService
from core.utils import create_jwt

HOUR_MS = 3_600_000
SWITCHES = {
    "DELAY_PREVIEW_ENABLED": True,
    "DELAY_PREVIEW_DAILY_BUILD_CAP": 150,
    "DELAY_PREVIEW_MINUTE_BUILD_CAP": 6,
    "AERODATABOX_SERVICE_URL": "http://fetcher.test/",
    "CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED": True,
    "CLOSABLE_FLOW_NONPRODUCTION_ENROLLMENT_ENABLED": True,
    "CLOSABLE_FLOW_TEST_PERCENT": 50,
    "CLOSABLE_FLOW_NONPRODUCTION_TEST_PERCENT": 50,
    "CLOSABLE_FLOW_MEASUREMENT_REVISION": 1,
    "CLOSABLE_FLOW_FORCE_CONTROL_DELIVERY": False,
    "CLOSABLE_FLOW_LIFETIME_ENABLED": False,
}
HISTORY = {
    "origins": [{"scheduledHourUtc": 23, "medianDelay": "00:22:00", "numConsideredFlights": 41,
                 "delayPercentiles": [{"percentile": 75, "delay": "00:38:00"}, {"percentile": 90, "delay": "01:05:00"}]}],
    "destinations": [{"scheduledHourUtc": 6, "medianDelay": "00:18:00", "numConsideredFlights": 44}],
}
DEPARTURE_AIRPORT = {"departuresDelayInformation": {"delayIndex": 1.5, "medianDelay": "00:12:00"}}
ARRIVAL_AIRPORT = {"arrivalsDelayInformation": {"delayIndex": 0.8, "medianDelay": "00:04:00"}}


def installation_in(arm):
    while True:
        installation = uuid4()
        if policy.assigned_variant(installation, 50) == arm:
            return installation


class DelayPreviewTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.database = Path(self.scratch.name) / "preview.db"
        self.engine = self.make_engine()
        SQLModel.metadata.create_all(self.engine)
        self.stack = ExitStack()
        for key, value in SWITCHES.items():
            self.stack.enter_context(patch.object(settings, key, value))
        # Noon UTC today: the tests advance this clock without crossing a day.
        noon = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        self.clock = [int(noon.timestamp() * 1000)]
        self.stack.enter_context(patch.object(delay_preview, "current_time_ms", lambda: self.clock[0]))
        self.calls = []
        self.responses = {
            ("flight-delays", "BA178"): (200, HISTORY),
            ("airport-delay", "JFK"): (200, DEPARTURE_AIRPORT),
            ("airport-delay", "LHR"): (200, ARRIVAL_AIRPORT),
        }
        self.provider = httpx.AsyncClient(transport=httpx.MockTransport(self.fetcher))
        self.stack.enter_context(patch.object(delay_preview, "_client", self.provider))
        self.clients = []
        self.client = self.client_for(self.engine)

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.stack.close()
        asyncio.run(self.provider.aclose())
        self.engine.dispose()
        self.scratch.cleanup()

    # ------------------------------------------------------------ helpers

    def make_engine(self):
        return create_engine(f"sqlite:///{self.database}", connect_args={"check_same_thread": False, "timeout": 10},
                             hide_parameters=True)

    def client_for(self, engine):
        """One app per engine: a second engine on the same file is another worker."""
        app = FastAPI()
        app.include_router(flights.router, prefix="/flights")
        app.include_router(users.router, prefix="/users")
        app.include_router(activation_journey.router, prefix="/subscriptions")

        def session_dependency():
            with Session(engine) as session:
                yield session

        app.dependency_overrides[get_session] = session_dependency
        client = TestClient(app)
        self.clients.append(client)
        return client

    def fetcher(self, request: httpx.Request):
        path = request.url.path.strip("/")
        key = request.url.params.get("full_number") or request.url.params.get("airport_iata")
        self.calls.append((path, key))
        status_code, payload = self.responses.get((path, key), (200, {}))
        if isinstance(status_code, Exception):
            raise status_code
        return httpx.Response(status_code, json=payload)

    def guest(self, premium_until=None):
        user_id = str(uuid4())
        with Session(self.engine) as session:
            session.add(User(id=user_id, premium_valid_until=premium_until))
            session.commit()
        return user_id

    @staticmethod
    def headers(user_id):
        return {"Authorization": "Bearer " + create_jwt(sub=user_id)}

    def assign(self, user_id, arm=policy.TEST, *, enabled=True, client=None):
        installation = installation_in(arm)
        body = {"installation_id": str(installation), "enrollment_event_id": str(uuid4()),
                "enrolled_at_ms": int(time.time() * 1000) - HOUR_MS, "app_version": "3.10.0",
                "build_number": "146", "analytics_environment": "production", "is_new_installation": True}
        with ExitStack() as switches:
            if not enabled:
                switches.enter_context(patch.object(settings, "CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED", False))
            response = (client or self.client).post("/subscriptions/closable-flow/assignment", json=body,
                                                    headers=self.headers(user_id))
        self.assertEqual(response.status_code, 200, response.text)
        journey = response.json()["journey"]
        self.assertEqual(journey["variant"], arm if enabled else policy.CONTROL)
        return str(installation)

    def enroll_test_arm(self):
        user_id = self.guest()
        return user_id, self.assign(user_id)

    def today(self, offset_days=0):
        moment = datetime.fromtimestamp(self.clock[0] / 1000, tz=timezone.utc) + timedelta(days=offset_days)
        return moment.date().isoformat()

    def body(self, installation, *, number="BA178", day=None, departure="JFK", arrival="LHR", status="Expected",
             flight_id=None, departure_times=None, arrival_times=None, identity=None, **extra):
        day = day or self.today()
        payload = {
            "installation_id": installation,
            "flight_identity": identity or flight_identity(number, day, departure, arrival),
            "flight": {
                "number": number, "date": day, "departure_iata": departure, "arrival_iata": arrival, "status": status,
                "departure": departure_times or {"scheduled_utc": f"{day} 23:00Z", "revised_utc": None,
                                                 "predicted_utc": None, "runway_utc": None},
                "arrival": arrival_times or {"scheduled_utc": f"{day} 23:55Z", "revised_utc": None,
                                             "predicted_utc": None, "runway_utc": None},
            },
            **extra,
        }
        if flight_id is not None:
            payload["flight_id"] = flight_id
        return payload

    def preview(self, user_id, installation, client=None, **changes):
        return (client or self.client).post("/flights/delay-preview", json=self.body(installation, **changes),
                                            headers=self.headers(user_id))

    def rows(self, model):
        with Session(self.engine) as session:
            return session.exec(select(model)).all()

    def claim(self, user_id):
        with Session(self.engine) as session:
            return session.get(DelayPreviewClaim, user_id)

    def budget(self):
        return {row.bucket.split(":")[0]: row.used for row in self.rows(DelayPreviewBudget)}

    def cache_keys(self):
        return {row.key.split(":")[0] + ":" + row.key.split(":")[1][:8] for row in self.rows(DelayPreviewCache)}

    def stored_flight(self, *, flight_id=48213, status="Expected", runway=None, number="BA178", day=None):
        day = day or self.today()
        with Session(self.engine) as session:
            jfk = Airport(iata="JFK", name="John F Kennedy", municipality_name="New York", lat=40.6, lon=-73.8,
                          country_code="US")
            lhr = Airport(iata="LHR", name="Heathrow", municipality_name="London", lat=51.5, lon=-0.5,
                          country_code="GB")
            flight = Flight(id=flight_id, date=day, number=number, status=status)
            flight.departure = Departure(airport=jfk, scheduled_time_utc=f"{day} 23:00Z", runway_time_utc=runway)
            flight.arrival = Arrival(airport=lhr, scheduled_time_utc=f"{day} 23:55Z")
            session.add(flight)
            session.commit()
        return flight_id

    def snapshot(self):
        with Session(self.engine) as session:
            return {
                "flights": [(row.id, row.status, row.has_subscribed, row.subscription_id)
                            for row in session.exec(select(Flight)).all()],
                "links": session.exec(select(UserFlightLink)).all(),
                "subscriptions": session.exec(select(Subscription)).all(),
                "subscription_links": session.exec(select(UserSubscriptionLink)).all(),
                "premium": [(row.id, row.premium_valid_until) for row in session.exec(select(User)).all()],
            }

    # ------------------------------------------------------------ identity

    def test_identity_mirrors_the_app_policy(self):
        vector = "6c49e6e73f58f7542fce111a27a8339114a3e1714937dac5dd7ccf2f6f1d660c"
        self.assertEqual(flight_identity("BA178", "2026-10-22", "JFK", "LHR"), vector)
        self.assertEqual(flight_identity(" ba 178 ", "2026-10-22", " jfk", "lhr "), vector)
        for number, day, departure, arrival in (
            ("1234", "2026-10-22", "JFK", "LHR"), ("B1", "2026-10-22", "JFK", "LHR"),
            ("BA123456", "2026-10-22", "JFK", "LHR"), ("BA-178", "2026-10-22", "JFK", "LHR"),
            ("BA178", "2026-02-30", "JFK", "LHR"), ("BA178", "2026-10-22", "JF", "LHR"),
            ("BA178", "2026-10-22", "JFK", "L1R"), ("BA178", "2026-10-22T00:00", "JFK", "LHR"),
            ("BA178", "2026-10-22\n", "JFK", "LHR"), (None, "2026-10-22", "JFK", "LHR"),
        ):
            with self.subTest(number=number, day=day, departure=departure, arrival=arrival):
                self.assertIsNone(flight_identity(number, day, departure, arrival))
        self.assertIsNotNone(flight_identity("U21234", "2026-10-22", "LGW", "BCN"))
        self.assertIsNotNone(flight_identity("9W123A", "2026-10-22", "BOM", "DEL"))
        self.assertIsNotNone(flight_identity("BA12345", "2026-10-22", "JFK", "LHR"))  # "BA1" + "2345", as in the app

    # ------------------------------------------------------------ happy path

    def test_free_test_arm_guest_gets_a_real_prediction_without_tracking_or_premium(self):
        user_id, installation = self.enroll_test_arm()
        before = self.snapshot()
        with patch.object(flights, "create_webhook_for_flight", side_effect=AssertionError("no webhook")), \
             patch.object(flights.FlightService, "get_flights", side_effect=AssertionError("no flight lookup")):
            response = self.preview(user_id, installation)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["state"], body["cache"]), ("ready", "computed"))
        self.assertEqual(body["flight_identity"], flight_identity("BA178", self.today(), "JFK", "LHR"))
        prediction = body["prediction"]
        self.assertEqual(set(prediction), {"flight_id", "level", "score", "confidence", "confidence_score",
                                           "likely_delay_min", "likely_delay_max", "summary", "reasons",
                                           "data_points", "generated_at"})
        self.assertEqual((prediction["flight_id"], prediction["level"]), (0, "medium"))
        self.assertTrue(prediction["data_points"]["flight_history_available"])
        self.assertEqual(body["primary_reason"], "Historical median delay for this flight is 20 min.")
        self.assertEqual(sorted(self.calls), [("airport-delay", "JFK"), ("airport-delay", "LHR"),
                                              ("flight-delays", "BA178")])
        self.assertEqual(self.snapshot(), before)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(User, user_id).premium_valid_until)
        claim = self.claim(user_id)
        self.assertEqual((claim.state, claim.attempts, claim.installation_id, claim.analytics_environment),
                         ("ready", 1, installation, "production"))
        self.assertNotIn(user_id, claim.result_json)
        self.assertNotIn(installation, claim.result_json)
        self.assertEqual(self.budget(), {"day": 1, "minute": 1})

    def test_known_flight_row_is_authoritative_and_never_linked(self):
        user_id, installation = self.enroll_test_arm()
        flight_id = self.stored_flight()
        before = self.snapshot()
        # The descriptor's forged delay is ignored when a row exists.
        response = self.preview(user_id, installation, flight_id=flight_id,
                                departure_times={"scheduled_utc": f"{self.today()} 23:00Z",
                                                 "revised_utc": f"{self.today()} 23:59Z"})
        self.assertEqual(response.status_code, 200, response.text)
        prediction = response.json()["prediction"]
        self.assertEqual(prediction["flight_id"], flight_id)
        self.assertIsNone(prediction["data_points"]["current_departure_delay_minutes"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.claim(user_id).flight_id, flight_id)

    # ------------------------------------------------------------ eligibility

    def test_only_an_owned_server_randomized_test_arm_is_eligible(self):
        control_user = self.guest()
        control = self.assign(control_user, policy.CONTROL)
        disabled_user = self.guest()
        disabled = self.assign(disabled_user, policy.TEST, enabled=False)
        owner, owned = self.enroll_test_arm()
        stranger = self.guest()
        cases = [
            ("control", control_user, control),
            ("server_disabled", disabled_user, disabled),
            ("foreign_installation", stranger, owned),
            ("unknown_installation", stranger, str(uuid4())),
        ]
        for label, user_id, installation in cases:
            with self.subTest(label):
                response = self.preview(user_id, installation)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {"detail": {"code": "delay_preview_not_eligible", "retryable": False}})
        with patch.object(settings, "CLOSABLE_FLOW_FORCE_CONTROL_DELIVERY", True):
            response = self.preview(owner, owned)
        self.assertEqual(response.status_code, 403)
        # A premium account outside the test arm is not eligible either.
        premium = self.guest(premium_until=int(time.time()) + 86_400)
        self.assertEqual(self.preview(premium, str(uuid4())).status_code, 403)
        self.assertEqual(self.rows(DelayPreviewClaim), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.preview(owner, owned).status_code, 200)

    def test_invalid_token_is_unauthorized(self):
        _, installation = self.enroll_test_arm()
        response = self.client.post("/flights/delay-preview", json=self.body(installation),
                                    headers={"Authorization": "Bearer not-a-token"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.rows(DelayPreviewClaim), [])

    def test_kill_switch_unknown_departed_and_invalid_flights_are_refused_without_a_claim(self):
        user_id, installation = self.enroll_test_arm()
        with patch.object(settings, "DELAY_PREVIEW_ENABLED", False):
            response = self.preview(user_id, installation)
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (503, "delay_preview_disabled"))

        departed_id = self.stored_flight(flight_id=777, status="Departed")
        runway_id = self.stored_flight(flight_id=778, runway=f"{self.today()} 23:04Z")
        other_number_id = self.stored_flight(flight_id=779, number="BA179")
        not_eligible = {
            "airborne_status": dict(status="EnRoute"),
            "departure_runway_time": dict(departure_times={"scheduled_utc": f"{self.today()} 23:00Z",
                                                           "runway_utc": f"{self.today()} 23:04Z"}),
            "departed_row": dict(flight_id=departed_id),
            "row_with_runway_time": dict(flight_id=runway_id),
        }
        for label, changes in not_eligible.items():
            with self.subTest(label):
                response = self.preview(user_id, installation, **changes)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"]["code"], "delay_preview_flight_not_eligible")
        invalid = {
            "unknown_flight_id": dict(flight_id=9_999_999),
            "row_identity_mismatch": dict(flight_id=other_number_id),
            "identity_mismatch": dict(identity="0" * 64),
            "bad_number": dict(number="BA-178", identity="0" * 64),
            "unknown_status": dict(status="Teleported"),
            "date_too_old": dict(day=self.today(-5)),
            "date_too_far": dict(day=self.today(400)),
            "bad_time_format": dict(departure_times={"scheduled_utc": "tomorrow"}),
            "extra_field": dict(email="someone@example.invalid"),
            "string_flight_id": dict(flight_id="48213"),
        }
        for label, changes in invalid.items():
            with self.subTest(label):
                response = self.preview(user_id, installation, **changes)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {"detail": {"code": "delay_preview_invalid_flight", "retryable": False}})
        self.assertEqual(self.rows(DelayPreviewClaim), [])
        self.assertEqual(self.calls, [])

    # ------------------------------------------------------------ one per user

    def test_same_flight_returns_the_stored_result_without_provider_or_budget(self):
        user_id, installation = self.enroll_test_arm()
        day = self.today()
        first = self.preview(user_id, installation, day=day).json()
        calls, budget = list(self.calls), self.budget()
        # Even after every shared cache row expires.
        self.clock[0] += 13 * HOUR_MS
        again = self.preview(user_id, installation, day=day)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json(), {**first, "cache": "stored"})
        self.assertEqual((self.calls, self.budget()), (calls, budget))

    def test_any_other_flight_is_limited_for_the_account_and_the_installation(self):
        user_id, installation = self.enroll_test_arm()
        self.assertEqual(self.preview(user_id, installation).status_code, 200)
        calls = list(self.calls)
        for label, changes in {"other_number": dict(number="BA179"), "other_day": dict(day=self.today(1)),
                               "other_route": dict(arrival="LGW")}.items():
            with self.subTest(label):
                response = self.preview(user_id, installation, **changes)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.json(), {"detail": {"code": "delay_preview_limit_reached", "retryable": False}})
        # A second installation of the same account is still one account.
        second_installation = self.assign(user_id)
        self.assertEqual(self.preview(user_id, second_installation, number="BA179").status_code, 429)
        self.assertEqual(self.preview(user_id, second_installation).json()["cache"], "stored")
        # Another account on this installation fails eligibility (the assignment
        # is owned), and the claim's installation key also binds it.
        other = self.guest()
        self.assertEqual(self.preview(other, installation, number="BA179").status_code, 403)
        self.assertEqual(self.calls, calls)
        identity = flight_identity("BA178", self.today(), "JFK", "LHR")
        other_identity = flight_identity("BA179", self.today(), "JFK", "LHR")
        with Session(self.engine) as session:
            bind = lambda value: delay_preview._bind(  # noqa: E731
                session, user_id=other, installation=installation, identity=value, flight_id=None,
                environment="production", now_ms=self.clock[0])
            self.assertEqual(bind(other_identity).status_code, 429)
            stored = bind(identity)
            self.assertEqual((stored.status_code, stored.body["cache"]), (200, "stored"))
        self.assertEqual(len(self.rows(DelayPreviewClaim)), 1)

    def test_racing_workers_bind_exactly_one_flight(self):
        user_id, installation = self.enroll_test_arm()
        barrier = threading.Barrier(2, timeout=10)
        seen = threading.local()
        original = delay_preview._claims

        def racing_claims(*args, **kwargs):
            rows = original(*args, **kwargs)
            if not getattr(seen, "waited", False):
                seen.waited = True
                barrier.wait()  # both workers read "no claim" before either inserts
            return rows

        def worker(number, results):
            engine = self.make_engine()  # one engine per worker process
            try:
                with Session(engine) as session:
                    user = session.get(User, user_id)
                    outcome = asyncio.run(request_delay_preview(
                        session=session, user=user, payload=self.body(installation, number=number)))
                    results.append(outcome)
            finally:
                engine.dispose()

        for numbers, expected in ((("BA178", "BA179"), "limit"), (("BA178", "BA178"), "same_flight")):
            with self.subTest(numbers=numbers):
                with Session(self.engine) as session:
                    for row in session.exec(select(DelayPreviewClaim)).all():
                        session.delete(row)
                    session.commit()
                barrier.reset()
                results = []
                with patch.object(delay_preview, "_claims", racing_claims):
                    threads = [threading.Thread(target=worker, args=(number, results)) for number in numbers]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(20)
                statuses = sorted(result.status_code for result in results)
                claims = self.rows(DelayPreviewClaim)
                self.assertEqual(len(claims), 1)
                winner = next(result for result in results if result.body.get("cache") in ("computed", "flight_cache"))
                self.assertEqual(claims[0].flight_identity, winner.body["flight_identity"])
                loser = next(result for result in results if result is not winner)
                if expected == "limit":
                    self.assertEqual(statuses, [200, 429])
                else:
                    # The loser sees the winner's lease (202) or, if the build
                    # already finished, the stored result; never a second build.
                    self.assertIn((loser.status_code, loser.body.get("cache", "none")), ((202, "none"), (200, "stored")))

    # ------------------------------------------------------------ caches

    def test_shared_caches_serve_other_accounts_and_workers_with_their_ttls(self):
        first_user, first_installation = self.enroll_test_arm()
        self.assertEqual(self.preview(first_user, first_installation).json()["cache"], "computed")
        with Session(self.engine) as session:
            ttls = {row.key.split(":")[0]: row.expires_at_ms - row.created_at_ms
                    for row in session.exec(select(DelayPreviewCache)).all()}
        self.assertEqual(ttls, {"result": HOUR_MS, "airport": HOUR_MS // 2, "history": 12 * HOUR_MS})
        calls, budget = list(self.calls), self.budget()

        # Another account served by another worker (its own engine and pool).
        other_engine = self.make_engine()
        try:
            other_client = self.client_for(other_engine)
            second_user = self.guest()
            second_installation = self.assign(second_user, client=other_client)
            shared = self.preview(second_user, second_installation, client=other_client)
            self.assertEqual(shared.status_code, 200)
            self.assertEqual((shared.json()["cache"], shared.json()["state"]), ("flight_cache", "ready"))
            self.assertEqual((self.calls, self.budget()), (calls, budget))
            self.assertEqual(self.claim(second_user).attempts, 0)
            self.assertEqual(self.preview(second_user, second_installation, client=other_client).json()["cache"],
                             "stored")
        finally:
            other_engine.dispose()

        # Same number and airports on another day: composed from cached inputs.
        third_user, third_installation = self.enroll_test_arm()
        response = self.preview(third_user, third_installation, day=self.today(1))
        self.assertEqual((response.json()["cache"], self.calls, self.budget()), ("computed", calls, budget))
        # Same number, another arrival airport: exactly one provider call.
        fourth_user, fourth_installation = self.enroll_test_arm()
        self.assertEqual(self.preview(fourth_user, fourth_installation, arrival="LGW").status_code, 200)
        self.assertEqual(self.calls[len(calls):], [("airport-delay", "LGW")])

        # After 31 minutes the airport inputs refresh; history (12 h) does not.
        self.clock[0] += 31 * 60_000
        fifth_user, fifth_installation = self.enroll_test_arm()
        before = len(self.calls)
        self.assertEqual(self.preview(fifth_user, fifth_installation, day=self.today(2)).status_code, 200)
        self.assertEqual(sorted(self.calls[before:]), [("airport-delay", "JFK"), ("airport-delay", "LHR")])
        # After 61 minutes the shared result for the first flight has expired too.
        self.clock[0] += 30 * 60_000
        sixth_user, sixth_installation = self.enroll_test_arm()
        self.assertEqual(self.preview(sixth_user, sixth_installation).json()["cache"], "computed")
        # Expired rows are pruned opportunistically.
        with Session(self.engine) as session:
            live = session.exec(select(DelayPreviewCache)).all()
        self.assertTrue(all(row.expires_at_ms > self.clock[0] for row in live))

    def test_forged_descriptor_never_reaches_another_account(self):
        forger, forger_installation = self.enroll_test_arm()
        day = self.today()
        forged = self.preview(forger, forger_installation, departure_times={
            "scheduled_utc": f"{day} 23:00Z", "revised_utc": f"{day} 23:59Z"}).json()
        self.assertEqual(forged["prediction"]["data_points"]["current_departure_delay_minutes"], 59)
        honest_user, honest_installation = self.enroll_test_arm()
        honest = self.preview(honest_user, honest_installation).json()
        self.assertEqual(honest["cache"], "computed")
        self.assertIsNone(honest["prediction"]["data_points"]["current_departure_delay_minutes"])
        self.assertLess(honest["prediction"]["score"], forged["prediction"]["score"])
        results = [row.key for row in self.rows(DelayPreviewCache) if row.key.startswith("result:")]
        self.assertEqual(len(results), 2)

    # ------------------------------------------------------------ global cap

    def test_global_caps_bound_uncached_builds_only(self):
        first_user, first_installation = self.enroll_test_arm()
        with patch.object(settings, "DELAY_PREVIEW_DAILY_BUILD_CAP", 1):
            self.assertEqual(self.preview(first_user, first_installation).status_code, 200)
            second_user, second_installation = self.enroll_test_arm()
            refused = self.preview(second_user, second_installation, number="LH400", departure="FRA", arrival="JFK")
            self.assertEqual(refused.status_code, 503)
            self.assertEqual(refused.json(), {"detail": {"code": "delay_preview_capacity", "retryable": True}})
            now = datetime.fromtimestamp(self.clock[0] / 1000, tz=timezone.utc)
            midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
            self.assertAlmostEqual(int(refused.headers["Retry-After"]), (midnight - now).total_seconds(), delta=2)
            claim = self.claim(second_user)
            self.assertEqual((claim.state, claim.failure_reason, claim.attempts), ("failed", "capacity", 0))
            # Cache hits stay free while the cap is exhausted.
            third_user, third_installation = self.enroll_test_arm()
            self.assertEqual(self.preview(third_user, third_installation).json()["cache"], "flight_cache")
        self.assertEqual(self.budget(), {"day": 1, "minute": 1})
        calls = len(self.calls)
        # The capacity refusal stays retryable for the same flight, without a cooldown.
        retried = self.preview(second_user, second_installation, number="LH400", departure="FRA", arrival="JFK")
        self.assertEqual((retried.status_code, retried.json()["cache"]), (200, "computed"))
        self.assertEqual(sorted(self.calls[calls:]), [("airport-delay", "FRA"), ("flight-delays", "LH400")])
        self.assertEqual(self.claim(second_user).attempts, 1)

        # Minute cap: refused with the seconds to the next minute, day bucket untouched.
        with patch.object(settings, "DELAY_PREVIEW_MINUTE_BUILD_CAP", 2):
            fourth_user, fourth_installation = self.enroll_test_arm()
            refused = self.preview(fourth_user, fourth_installation, number="AF22", departure="CDG", arrival="JFK")
        self.assertEqual(refused.status_code, 503)
        self.assertEqual(refused.json()["detail"]["code"], "delay_preview_capacity")
        self.assertTrue(1 <= int(refused.headers["Retry-After"]) <= 60)
        self.assertEqual(self.budget(), {"day": 2, "minute": 2})
        with patch.object(settings, "DELAY_PREVIEW_DAILY_BUILD_CAP", 0):
            fifth_user, fifth_installation = self.enroll_test_arm()
            self.assertEqual(self.preview(fifth_user, fifth_installation, number="AF23", departure="CDG",
                                          arrival="JFK").status_code, 503)

    # ------------------------------------------------------------ honesty

    def test_provider_failures_are_retryable_twice_for_the_same_flight_and_never_cached(self):
        user_id, installation = self.enroll_test_arm()
        for key in list(self.responses):
            self.responses[key] = (500, {"message": "provider said BA178 JFK quota"})
        first = self.preview(user_id, installation)
        self.assertEqual(first.status_code, 503)
        self.assertEqual(first.json(), {"detail": {"code": "delay_preview_unavailable", "retryable": True}})
        self.assertEqual(first.headers["Retry-After"], "60")
        self.assertNotIn("BA178", first.text)
        claim = self.claim(user_id)
        self.assertEqual((claim.state, claim.failure_reason, claim.attempts), ("failed", "provider_unavailable", 1))
        self.assertEqual(self.rows(DelayPreviewCache), [])
        self.assertEqual(len(self.calls), 3)

        self.clock[0] += 30_000
        cooling = self.preview(user_id, installation)
        self.assertEqual((cooling.status_code, cooling.json()["detail"]["retryable"]), (503, True))
        self.assertTrue(25 <= int(cooling.headers["Retry-After"]) <= 30)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.preview(user_id, installation, number="BA179").status_code, 429)

        self.clock[0] += 31_000
        self.responses[("flight-delays", "BA178")] = (429, {})
        second = self.preview(user_id, installation)
        self.assertEqual(second.json(), {"detail": {"code": "delay_preview_unavailable", "retryable": False}})
        self.assertNotIn("Retry-After", second.headers)
        claim = self.claim(user_id)
        self.assertEqual((claim.failure_reason, claim.attempts), ("provider_rate_limited", 2))
        self.assertEqual(len(self.calls), 6)

        self.clock[0] += 120_000
        final = self.preview(user_id, installation)
        self.assertEqual(final.json(), {"detail": {"code": "delay_preview_unavailable", "retryable": False}})
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(self.rows(DelayPreviewCache), [])

    def test_transport_errors_and_the_deadline_are_timeouts(self):
        user_id, installation = self.enroll_test_arm()
        request = httpx.Request("GET", "http://fetcher.test/")
        for key in list(self.responses):
            self.responses[key] = (httpx.ConnectTimeout("connect timed out", request=request), None)
        response = self.preview(user_id, installation)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.claim(user_id).failure_reason, "timeout")

        async def slow(client, key):
            await asyncio.sleep(5)
            return "ok", {}

        second_user, second_installation = self.enroll_test_arm()
        with patch.object(delay_preview, "PROVIDER_DEADLINE_SECONDS", 0.05), \
             patch.object(delay_preview, "_fetch_json", slow):
            started = time.perf_counter()
            response = self.preview(second_user, second_installation)
        self.assertLess(time.perf_counter() - started, 3)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.claim(second_user).failure_reason, "timeout")

    def test_empty_provider_data_is_insufficient_never_low_risk(self):
        user_id, installation = self.enroll_test_arm()
        for key in list(self.responses):
            self.responses[key] = (200, {})
        response = self.preview(user_id, installation)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"state": "insufficient_data", "cache": "computed",
                                           "flight_identity": flight_identity("BA178", self.today(), "JFK", "LHR"),
                                           "primary_reason": None, "prediction": None})
        self.assertNotIn("low", response.text.lower())
        self.assertEqual(self.claim(user_id).state, "insufficient_data")
        again = self.preview(user_id, installation).json()
        self.assertEqual((again["state"], again["cache"]), ("insufficient_data", "stored"))
        self.assertEqual(self.preview(user_id, installation, number="BA179").status_code, 429)
        # A delayed status or a live delay is a real signal.
        delayed_user, delayed_installation = self.enroll_test_arm()
        delayed = self.preview(delayed_user, delayed_installation, status="Delayed").json()
        self.assertEqual(delayed["state"], "ready")
        self.assertEqual(delayed["primary_reason"], "AeroDataBox currently marks the flight as delayed.")

    def test_partial_failure_predicts_without_sharing_or_claiming_no_signal(self):
        user_id, installation = self.enroll_test_arm()
        self.responses[("airport-delay", "JFK")] = (503, {})
        response = self.preview(user_id, installation)
        self.assertEqual((response.status_code, response.json()["state"]), (200, "ready"))
        keys = {row.key.split(":")[0] + ":" + row.key.split(":")[1] for row in self.rows(DelayPreviewCache)}
        self.assertEqual(keys, {"history:BA178", "airport:LHR"})

        # History failed and the airports have no index: unknown, not "no signal".
        second_user, second_installation = self.enroll_test_arm()
        self.responses.update({("flight-delays", "LH400"): (500, {}), ("airport-delay", "FRA"): (200, {}),
                               ("airport-delay", "JFK"): (200, {})})
        unknown = self.preview(second_user, second_installation, number="LH400", departure="FRA", arrival="JFK")
        self.assertEqual(unknown.json(), {"detail": {"code": "delay_preview_unavailable", "retryable": True}})
        self.assertEqual(self.claim(second_user).state, "failed")

    def test_partial_failure_never_reads_as_low_risk_and_the_retry_fetches_only_what_failed(self):
        user_id, installation = self.enroll_test_arm()
        calm = {"departuresDelayInformation": {"delayIndex": 0.5}}
        self.responses.update({("flight-delays", "BA178"): (500, {}), ("airport-delay", "JFK"): (200, calm),
                               ("airport-delay", "LHR"): (200, ARRIVAL_AIRPORT)})
        first = self.preview(user_id, installation)
        # Calm airports alone would compose "low": with the history call failed
        # that is unknown, never a low risk.
        self.assertEqual(first.json(), {"detail": {"code": "delay_preview_unavailable", "retryable": True}})
        self.assertNotIn("low", first.text.lower())
        claim = self.claim(user_id)
        self.assertEqual((claim.state, claim.failure_reason, claim.attempts, claim.result_json),
                         ("failed", "provider_unavailable", 1, None))
        # History is asked first: the fetcher starts calls at 1 per second.
        self.assertEqual(self.calls[0], ("flight-delays", "BA178"))
        self.assertEqual(len(self.calls), 3)

        self.clock[0] += 61_000
        self.responses[("flight-delays", "BA178")] = (200, HISTORY)
        retried = self.preview(user_id, installation)
        self.assertEqual((retried.status_code, retried.json()["state"], retried.json()["cache"]), (200, "ready", "computed"))
        self.assertEqual(self.calls[3:], [("flight-delays", "BA178")])
        self.assertEqual(self.claim(user_id).attempts, 2)

    def test_pending_lease_answers_202_until_it_expires(self):
        user_id, installation = self.enroll_test_arm()
        identity = flight_identity("BA178", self.today(), "JFK", "LHR")
        with Session(self.engine) as session:
            session.add(DelayPreviewClaim(user_id=user_id, installation_id=installation, flight_identity=identity,
                                          state="pending", attempts=1, analytics_environment="production",
                                          pending_until_ms=self.clock[0] + 15_000, created_at_ms=self.clock[0],
                                          updated_at_ms=self.clock[0]))
            session.commit()
        pending = self.preview(user_id, installation)
        self.assertEqual(pending.status_code, 202)
        self.assertEqual(pending.json(), {"state": "pending", "cache": "none", "flight_identity": identity,
                                          "primary_reason": None, "prediction": None, "retry_after_ms": 1000})
        self.assertEqual(self.calls, [])
        self.clock[0] += 61_000  # the worker died: the lease expired and the cooldown passed
        rebuilt = self.preview(user_id, installation)
        self.assertEqual((rebuilt.status_code, rebuilt.json()["state"]), (200, "ready"))
        self.assertEqual(self.claim(user_id).attempts, 2)

    def test_primary_reason_priority(self):
        self.assertEqual(primary_reason([
            "Historical median delay for this flight is 22 min.", "Departure airport delay index is 1.5/5.",
            "Current arrival timing is already 12 min behind schedule.",
        ]), "Current arrival timing is already 12 min behind schedule.")
        self.assertEqual(primary_reason([
            "Departure airport delay index is 1.5/5.", "Arrival airport delay index is 2/5.",
            "Historical sample size is limited.",
        ]), "Arrival airport delay index is 2/5.")
        self.assertEqual(primary_reason([
            "Departure airport delay index is 2/5.", "Arrival airport delay index is 2/5.",
        ]), "Departure airport delay index is 2/5.")
        self.assertEqual(primary_reason(["Historical sample size is limited.",
                                         "No strong delay signals were found from current data."]),
                         "Historical sample size is limited.")
        self.assertIsNone(primary_reason(["Something new from a future scorer."]))

    # ------------------------------------------------------------ parity

    def test_premium_scoring_is_unchanged_by_the_compose_extraction(self):
        def deparr(iata, scheduled, revised=None, predicted=None, runway=None):
            return SimpleNamespace(airport=SimpleNamespace(iata=iata), scheduled_time_utc=scheduled,
                                   revised_time_utc=revised, predicted_time_utc=predicted, runway_time_utc=runway)

        # Golden outputs captured from build_delay_risk at backend HEAD 621b908.
        fixtures = {
            "rich": (SimpleNamespace(id=48213, number="BA 178", status="Expected",
                                     departure=deparr("JFK", "2026-10-22 23:00Z"),
                                     arrival=deparr("LHR", "2026-10-23 06:05Z")),
                     {"origins": [{"scheduledHourUtc": 22, "medianDelay": "00:22:00", "numConsideredFlights": 41,
                                   "delayPercentiles": [{"percentile": 75, "delay": "00:38:00"},
                                                        {"percentile": 90, "delay": "01:05:00"}]},
                                  {"scheduledHourUtc": 9, "medianDelay": "00:02:00", "numConsideredFlights": 90}],
                      "destinations": [{"scheduledHourUtc": 6, "medianDelay": "00:18:00",
                                        "numConsideredFlights": 44}]},
                     DEPARTURE_AIRPORT, ARRIVAL_AIRPORT,
                     '{"confidence": "high", "confidence_score": 0.9, "data_points": {"airport_delay_available": true, "arrival_airport_delay_index": 0.8, "current_arrival_delay_minutes": null, "current_departure_delay_minutes": null, "departure_airport_delay_index": 1.5, "destination_samples": 44, "flight_history_available": true, "historical_median_delay_minutes": 20, "origin_samples": 41}, "flight_id": 48213, "level": "medium", "likely_delay_max": 65, "likely_delay_min": 10, "reasons": ["Historical median delay for this flight is 20 min.", "75th percentile historical delay is 38 min.", "Departure airport delay index is 1.5/5.", "Arrival airport delay index is 0.8/5."], "score": 33, "summary": "Medium delay risk - likely 10-65 min"}'),
            "empty": (SimpleNamespace(id=None, number="LH400", status="Expected",
                                      departure=deparr("FRA", "2026-10-22 11:55Z"),
                                      arrival=deparr("JFK", "2026-10-22 20:40Z")),
                      {}, {}, {},
                      '{"confidence": "low", "confidence_score": 0.35, "data_points": {"airport_delay_available": false, "arrival_airport_delay_index": null, "current_arrival_delay_minutes": null, "current_departure_delay_minutes": null, "departure_airport_delay_index": null, "destination_samples": null, "flight_history_available": false, "historical_median_delay_minutes": null, "origin_samples": null}, "flight_id": 0, "level": "low", "likely_delay_max": 10, "likely_delay_min": 0, "reasons": ["No strong delay signals were found from current data."], "score": 0, "summary": "Low delay risk - likely on time"}'),
            "delayed": (SimpleNamespace(id=7, number="AF22", status="Delayed",
                                        departure=deparr("CDG", "2026-10-22 10:00Z", revised="2026-10-22 10:45Z"),
                                        arrival=deparr("JFK", "2026-10-22 18:00Z", predicted="2026-10-22T18:30Z")),
                        {"origin": {"medianDelay": "-00:07:00", "numConsideredFlights": 3}},
                        {"departures": {"delayIndex": "4.2", "medianDelay": "00:41:00"}},
                        {"arrivals": {"index": 2}},
                        '{"confidence": "high", "confidence_score": 0.77, "data_points": {"airport_delay_available": true, "arrival_airport_delay_index": 2.0, "current_arrival_delay_minutes": 30, "current_departure_delay_minutes": 45, "departure_airport_delay_index": 4.2, "destination_samples": null, "flight_history_available": true, "historical_median_delay_minutes": -7, "origin_samples": 3}, "flight_id": 7, "level": "high", "likely_delay_max": 46, "likely_delay_min": 26, "reasons": ["This flight has historically run 7 min early.", "Departure airport delay index is 4.2/5.", "Arrival airport delay index is 2/5.", "Current departure timing is already 45 min behind schedule."], "score": 81, "summary": "High delay risk - likely 26-46 min"}'),
        }
        for name, (flight, history, departure, arrival, golden) in fixtures.items():
            with self.subTest(name):
                async def flight_delays(_, full_number):
                    return history

                async def airport_delay(_, airport_iata, date_local=None):
                    return departure if airport_iata == flight.departure.airport.iata else arrival

                with patch.object(api_client.AerodataboxClient, "__init__", lambda client: None), \
                     patch.object(api_client.AerodataboxClient, "get_flight_delays", flight_delays), \
                     patch.object(api_client.AerodataboxClient, "get_airport_delay", airport_delay):
                    built = asyncio.run(DelayRiskService.build_delay_risk(flight)).model_dump()
                composed = DelayRiskService.compose(flight, history, departure, arrival).model_dump()
                for value in (built, composed):
                    value.pop("generated_at")
                    self.assertEqual(value, json.loads(golden))

    # ------------------------------------------------------------ privacy

    def test_logs_never_carry_identifiers_numbers_airports_times_or_tokens(self):
        user_id, installation = self.enroll_test_arm()
        flight_id = self.stored_flight()
        token = create_jwt(sub=user_id)
        day = self.today()
        identity = flight_identity("BA178", day, "JFK", "LHR")
        with self.assertLogs(level="DEBUG") as captured:
            self.preview(user_id, installation, flight_id=flight_id)  # ready
            self.preview(user_id, installation, flight_id=flight_id)  # stored
            self.preview(user_id, installation, number="BA179")       # limit
            self.preview(self.guest(), installation)                  # not eligible
            other_user, other_installation = self.enroll_test_arm()
            self.responses[("flight-delays", "LH400")] = (500, {})
            self.responses[("airport-delay", "FRA")] = (500, {})
            self.responses[("airport-delay", "JFK")] = (500, {})
            self.preview(other_user, other_installation, number="LH400", departure="FRA", arrival="JFK")
            with patch.object(delay_preview, "_build", side_effect=RuntimeError(f"boom {identity} BA178 JFK")):
                crash_user, crash_installation = self.enroll_test_arm()
                crashed = self.preview(crash_user, crash_installation)
        self.assertEqual(crashed.status_code, 500)
        self.assertEqual(crashed.json(), {"detail": "Internal server error"})
        messages = [record.getMessage() for record in captured.records]
        outcomes = [message for message in messages if message.startswith("delay_preview outcome=")]
        self.assertEqual([message.split()[1] for message in outcomes],
                         ["outcome=ready", "outcome=stored", "outcome=limit", "outcome=not_eligible",
                          "outcome=unavailable", "outcome=error"])
        self.assertIn("delay_preview_failed stage=handler error=RuntimeError", messages)
        # The test client's own httpx lines name only its routes; the fetcher's are quiet.
        self.assertFalse([record for record in captured.records if "fetcher.test" in record.getMessage()])
        self.assertTrue([record for record in captured.records if "testserver/flights/delay-preview" in record.getMessage()])
        forbidden = [user_id, other_user, crash_user, installation, other_installation, crash_installation, identity,
                     "BA178", "BA179", "LH400", str(flight_id), "JFK", "LHR", "FRA", day, "23:00", token]
        for message in messages:
            for value in forbidden:
                self.assertNotIn(value, message)

    def test_account_deletion_removes_the_claim(self):
        user_id, installation = self.enroll_test_arm()
        self.assertEqual(self.preview(user_id, installation).status_code, 200)
        response = self.client.delete("/users/me/", headers=self.headers(user_id))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.claim(user_id))

    # ------------------------------------------------------------ contract surface

    def test_settings_defaults_and_bounds(self):
        defaults = Settings()
        self.assertEqual((defaults.DELAY_PREVIEW_ENABLED, defaults.DELAY_PREVIEW_DAILY_BUILD_CAP,
                          defaults.DELAY_PREVIEW_MINUTE_BUILD_CAP), (False, 150, 6))
        for changes in ({"DELAY_PREVIEW_DAILY_BUILD_CAP": -1}, {"DELAY_PREVIEW_DAILY_BUILD_CAP": 5001},
                        {"DELAY_PREVIEW_MINUTE_BUILD_CAP": 61}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                Settings(**changes)

    def test_openapi_lists_the_route_without_identifiers_in_the_path(self):
        from core.main import app

        paths = app.openapi()["paths"]
        self.assertIn("post", paths["/flights/delay-preview"])
        self.assertIn("post", paths["/flights/search/term"])  # the readiness gate's target

    def test_analytics_allowlist_accepts_the_onboarding_preview_source_only_with_its_codes(self):
        identity = flight_identity("BA178", "2026-10-22", "JFK", "LHR")

        def event(name, **properties):
            return DiagnosticEvent(
                event_id=uuid4(), event_name=name, occurred_at_ms=int(time.time() * 1000),
                installation_id=uuid4(), app_version="3.10.0", build_number="146",
                analytics_environment="production", build_configuration="release",
                properties={"event_schema_version": 28, **properties})

        loaded = dict(source="onboarding_preview", flight_identity=identity, level="medium", score=41,
                      confidence="high", outcome="computed", load_scope="initial_presentation")
        failed = dict(source="onboarding_preview", flight_identity=identity, reason="limit_reached",
                      load_scope="initial_presentation")
        event("delay_risk_loaded", **loaded)
        event("delay_risk_loaded", **{**loaded, "outcome": "stored", "load_scope": "after_fallback", "flight_id": 48213,
                                      "search_journey_id": str(uuid4()), "search_attempt_number": 2})
        for reason in ("insufficient_data", "pending_timeout", "cancelled", "decoding", "capacity"):
            event("delay_risk_failed", **{**failed, "reason": reason})
        # The paid card keeps its existing loose payload.
        event("delay_risk_loaded", source="flight_detail_preflight", flight_id=7, level="low", score=3,
              confidence="medium", event_schema_version=26)
        event("delay_risk_failed", source="flight_detail_preflight", flight_id=7, reason="http_error",
              event_schema_version=26)
        for name, properties in (
            ("delay_risk_loaded", {**loaded, "flight_identity": None}),
            ("delay_risk_loaded", {**loaded, "event_schema_version": 26}),
            ("delay_risk_loaded", {**loaded, "outcome": "fresh"}),
            ("delay_risk_loaded", {**loaded, "level": "severe"}),
            ("delay_risk_loaded", {**loaded, "load_scope": "refresh"}),
            ("delay_risk_loaded", {**loaded, "reason": "limit_reached"}),
            ("delay_risk_loaded", {key: value for key, value in loaded.items() if key != "score"}),
            ("delay_risk_failed", {**failed, "reason": "provider said no"}),
            ("delay_risk_failed", {**failed, "reason": "unknown_reason"}),
            ("delay_risk_failed", {**failed, "load_scope": "after_fallback"}),
            ("delay_risk_failed", {**failed, "level": "low"}),
        ):
            with self.subTest(name=name, properties=properties), self.assertRaises(ValidationError):
                event(name, **{key: value for key, value in properties.items() if value is not None})


class DelayPreviewMigrationTests(unittest.TestCase):
    def test_new_tables_are_created_with_their_unique_installation_key(self):
        engine = create_engine("sqlite://")
        try:
            SQLModel.metadata.create_all(engine)
            with Session(engine) as session:
                session.add(DelayPreviewClaim(user_id="a", installation_id="i", flight_identity="f" * 64,
                                              state="pending", analytics_environment="production",
                                              created_at_ms=1, updated_at_ms=1))
                session.commit()
                session.add(DelayPreviewClaim(user_id="b", installation_id="i", flight_identity="e" * 64,
                                              state="pending", analytics_environment="production",
                                              created_at_ms=1, updated_at_ms=1))
                with self.assertRaises(Exception):
                    session.commit()
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
