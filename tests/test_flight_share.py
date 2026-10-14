"""Synthetic-only flight-share wire tests: owner links, public page, privacy.

Legacy core.models initialization is confined to a scratch directory so it
never opens a developer database. Provider clients are patched to fail if a
share request ever reaches them.
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

_import_scratch = tempfile.TemporaryDirectory(prefix="sofly-flight-share-import-")
_previous_cwd = os.getcwd()
try:
    os.chdir(_import_scratch.name)
    from tests import test_experiment_reporting as _test_environment

    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import ValidationError
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session, SQLModel, create_engine, select

    from core.config import MAPLIBRE_CSS_INTEGRITY, MAPLIBRE_JS_INTEGRITY, settings
    from core.models import get_session
    from core.models.flight import Airline, Airport, Arrival, Departure, Flight
    from core.models.flight_share import FlightShare
    from core.models.user import User, UserFlightLink
    from core.routers import flight_share, flights, users
    from core.routers.experiment_diagnostics import DiagnosticEvent
    from core.security_logging import CredentialOperation
    from core.services import flight_share as share_service
    from core.services.flight.api_client import ADSBExchangeClient, AerodataboxClient, OpenSkyClient
    from core.services.flight_share_page import ShareTokenLogFilter, pick_language
    import core.main  # noqa: F401  (installs the access-log filter under test)
    from core.utils import create_jwt
finally:
    os.chdir(_previous_cwd)


OWNER = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
FLIGHT_ID = 987_654
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
CHROME = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile Safari/604.1"
BOT_AGENTS = (
    "WhatsApp/2.24.1 A", "facebookexternalhit/1.1", "Facebot", "Twitterbot/1.0",
    "Slackbot-LinkExpanding 1.0", "TelegramBot (like TwitterBot)",
    "Mozilla/5.0 (compatible; Discordbot/2.0)", "LinkedInBot/1.0", "SkypeUriPreview Preview/0.5",
    "Mozilla/5.0 (Macintosh) Applebot/0.1", "Googlebot/2.1", "example-crawler/1.0", "spider", "",
)
EXPECTED_HEADERS = {
    "cache-control": "no-store",
    "x-robots-tag": "noindex, nofollow, noarchive, nosnippet, noimageindex",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy": (
        "default-src 'none'; script-src 'self' https://cdnjs.cloudflare.com; "
        "style-src 'self' https://cdnjs.cloudflare.com; "
        "img-src 'self' data: blob: https://tiles.openfreemap.org; "
        "connect-src 'self' https://tiles.openfreemap.org; worker-src blob:; child-src blob:; "
        "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}
TIME_KEYS = {"utc", "local"}
AIRPORT_KEYS = {"iata", "name", "city", "country_code", "lat", "lon"}
LEG_KEYS = {
    "airport", "terminal", "gate", "checkin_desk", "baggage_belt",
    "scheduled", "revised", "predicted", "actual", "best", "delay_minutes",
}
FLIGHT_KEYS = {
    "number", "date", "status", "is_delayed", "airline", "aircraft", "distance_km", "departure", "arrival",
}
TOP_KEYS = {
    "schema_version", "state", "server_time_utc", "expires_at_utc", "refresh_after_seconds",
    "phase", "progress", "position", "flight",
}
FORBIDDEN_KEYS = {
    "id", "flight_id", "user_id", "user", "users", "share_id", "token", "token_hash", "subscription_id",
    "has_subscribed", "aircraft_modeS", "aircraft_mode_s", "quality", "view_count", "email", "full_name",
}
STATE_BLOCK = re.compile(r'<script type="application/json" id="sofly-share-state">(.*?)</script>', re.S)


def spec_token(share_id: str) -> str:
    # Independent restatement of the SPEC formula, not a call into the service.
    key = hmac.new(settings.JWT_SECRET.encode(), b"sofly-flight-share:v1", hashlib.sha256).digest()
    digest = hmac.new(key, share_id.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:22]


def walk(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield path + (key,), key, item
            yield from walk(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, path + (index,))


class TagCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def tags(page: str):
    parser = TagCollector()
    parser.feed(page)
    return parser.tags


def meta(page: str, attribute: str, name: str) -> str | None:
    for tag, attrs in tags(page):
        if tag == "meta" and attrs.get(attribute) == name:
            return attrs.get("content")
    return None


class FlightShareTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="sofly-flight-share-")
        self.engine = create_engine(
            f"sqlite:///{Path(self.scratch.name) / 'fixture.db'}",
            connect_args={"check_same_thread": False, "timeout": 10},
            hide_parameters=True,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(User(id=OWNER, verified=True))
            session.add(User(id=OTHER, verified=True))
            session.commit()
        self.patches = ExitStack()
        self.patches.enter_context(patch("core.models.engine", self.engine))
        self.now = self.patches.enter_context(patch.object(share_service, "utc_now", return_value=NOW))
        app = FastAPI()
        app.include_router(flights.router, prefix="/flights")
        app.include_router(flight_share.router, prefix="/flights")
        app.include_router(flight_share.public_router, prefix="/public/share")
        app.include_router(users.router, prefix="/users")

        def session_dependency():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_session] = session_dependency
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.patches.close()
        self.engine.dispose()
        self.scratch.cleanup()

    # ---------------- fixtures ------------------

    def headers(self, owner=OWNER):
        return {"Authorization": "Bearer " + create_jwt(sub=owner)}

    def add_flight(
        self, *, flight_id=FLIGHT_ID, owner=OWNER, status="EnRoute", airline_name="British Airways",
        departure=None, arrival=None, departure_airport=None, arrival_airport=None, date="2026-10-14",
    ):
        departure_times = {
            "scheduled_time_utc": "2026-10-14 09:00Z", "scheduled_time_local": "2026-10-14 10:00+01:00",
            "revised_time_utc": "not-a-time", "revised_time_local": None,
            "runway_time_utc": "2026-10-14 09:12Z", "runway_time_local": "2026-10-14 10:12+01:00",
        } if departure is None else departure
        arrival_times = {
            "scheduled_time_utc": "2026-10-14 17:00Z", "scheduled_time_local": "2026-10-14 13:00-04:00",
            "predicted_time_utc": "2026-10-14 16:05Z", "predicted_time_local": "2026-10-14 12:05-04:00",
        } if arrival is None else arrival
        with Session(self.engine) as session:
            flight = Flight(
                id=flight_id, date=date, number="BA178", status=status, distance_km=5540.4,
                subscription_id="PRIVATE_SUBSCRIPTION", has_subscribed=True,
                aircraft_reg="G-STBA", aircraft_modeS="400abc", aircraft_model="Boeing 777-300ER",
                airline=Airline(name=airline_name, iata="BA", icao="BAW"),
                departure=Departure(
                    terminal="5", gate="B32", checkin_desk="--", quality='["Basic","Live"]',
                    airport=departure_airport or Airport(
                        iata="LHR", name="London Heathrow", municipality_name="London",
                        lat=51.4706, lon=-0.461941, country_code="GB",
                    ),
                    **departure_times,
                ),
                arrival=Arrival(
                    terminal="7", baggage_belt="", quality='["Basic"]',
                    airport=arrival_airport or Airport(
                        iata="JFK", name="John F Kennedy International", municipality_name="New York",
                        lat=40.6398, lon=-73.7789, country_code="US",
                    ),
                    **arrival_times,
                ),
            )
            session.add(flight)
            if owner:
                session.add(UserFlightLink(user_id=owner, flight_id=flight_id))
            session.commit()
        return flight_id

    def share(self, flight_id=FLIGHT_ID, owner=OWNER):
        response = self.client.post(f"/flights/{flight_id}/share", headers=self.headers(owner))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def token(url: str) -> str:
        return url.rsplit("/", 1)[1]

    def page(self, token, **headers):
        return self.client.get(f"/public/share/{token}", headers={"User-Agent": CHROME, **headers})

    def live(self, token):
        return self.client.get(f"/public/share/{token}/live.json", headers={"User-Agent": CHROME})

    def rows(self):
        with Session(self.engine) as session:
            return session.exec(select(FlightShare).order_by(FlightShare.created_at_ms)).all()

    def assert_public_headers(self, response):
        for name, value in EXPECTED_HEADERS.items():
            self.assertEqual(response.headers.get(name), value, name)

    # ---------------- owner endpoints ------------------

    def test_owner_must_track_flight_and_unknown_flights_are_404(self):
        self.add_flight(owner=OTHER)
        for method in ("post", "get", "delete"):
            response = getattr(self.client, method)(f"/flights/{FLIGHT_ID}/share", headers=self.headers())
            self.assertEqual(response.status_code, 404, method)
        self.assertEqual(
            self.client.post(f"/flights/{FLIGHT_ID}/share", headers=self.headers()).json(),
            {"detail": "Flight not found"},
        )
        self.assertEqual(self.client.post("/flights/123456789/share", headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.post("/flights/0/share", headers=self.headers()).status_code, 422)
        self.assertIn(self.client.post(f"/flights/{FLIGHT_ID}/share").status_code, (401, 403))
        self.assertEqual(self.rows(), [])

    def test_create_reuse_get_and_response_contract(self):
        self.add_flight()
        first = self.share()
        self.assertEqual(set(first), {"url", "expires_at_utc", "created"})
        self.assertRegex(first["url"], r"^https://sofly\.to/f/[A-Za-z0-9_-]{22}$")
        self.assertEqual(first["expires_at_utc"], "2026-10-14T22:05:00Z")
        self.assertTrue(first["created"])
        again = self.share()
        self.assertEqual(again, {**first, "created": False})
        fetched = self.client.get(f"/flights/{FLIGHT_ID}/share", headers=self.headers())
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json(), {**first, "created": False})

        [row] = self.rows()
        token = self.token(first["url"])
        self.assertEqual(token, spec_token(row.id))
        self.assertRegex(row.id, r"^[0-9a-f]{32}$")
        self.assertEqual(row.token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, json.dumps(row.model_dump()))
        self.assertEqual((row.view_count, row.revoked_at_ms, row.revoke_reason), (0, None, None))
        self.assertNotIn(token, repr(flight_share.FlightShareLinkRead(**first)))

    def test_revoke_is_idempotent_and_recreate_issues_new_token(self):
        self.add_flight()
        old = self.token(self.share()["url"])
        self.assertEqual(self.page(old).status_code, 200)
        response = self.client.delete(f"/flights/{FLIGHT_ID}/share", headers=self.headers())
        self.assertEqual((response.status_code, response.json()), (200, {"revoked": True}))
        self.assertEqual(self.client.delete(f"/flights/{FLIGHT_ID}/share", headers=self.headers()).json(),
                         {"revoked": False})
        [row] = self.rows()
        self.assertIsNotNone(row.revoked_at_ms)
        self.assertEqual(row.revoke_reason, "owner")
        self.assertEqual(self.client.get(f"/flights/{FLIGHT_ID}/share", headers=self.headers()).status_code, 404)
        self.assertEqual(self.page(old).status_code, 404)
        self.assertEqual(self.live(old).json(), {"state": "not_found"})

        recreated = self.share()
        self.assertTrue(recreated["created"])
        new = self.token(recreated["url"])
        self.assertNotEqual(new, old)
        self.assertEqual(self.page(new).status_code, 200)
        self.assertEqual(self.page(old).status_code, 404)
        self.assertEqual(len(self.rows()), 2)

    def test_ended_flight_cannot_be_shared(self):
        self.add_flight(status="Arrived", arrival={
            "scheduled_time_utc": "2026-10-14 05:30Z", "runway_time_utc": "2026-10-14 05:59Z",
        })
        response = self.client.post(f"/flights/{FLIGHT_ID}/share", headers=self.headers())
        self.assertEqual((response.status_code, response.json()), (409, {"detail": "flight_ended"}))
        self.assertEqual(self.rows(), [])

    def test_expiry_uses_best_arrival_then_departure_then_service_date(self):
        def flight(departure=None, arrival=None, date="2026-10-14"):
            return Flight(
                id=1, date=date, number="BA178", status="Expected",
                departure=Departure(**(departure or {})), arrival=Arrival(**(arrival or {})),
            )

        expires = share_service.share_expires_at
        utc = lambda value: datetime.fromisoformat(value)  # noqa: E731
        self.assertEqual(expires(flight(arrival={
            "scheduled_time_utc": "2026-10-14 17:00Z", "revised_time_utc": "2026-10-14 17:10Z",
            "predicted_time_utc": "2026-10-14 16:50Z", "runway_time_utc": "2026-10-14 16:45Z",
        })), utc("2026-10-14T22:45:00+00:00"))
        self.assertEqual(expires(flight(arrival={
            "scheduled_time_utc": "2026-10-14 17:00Z", "revised_time_utc": "2026-10-14 17:10Z",
            "predicted_time_utc": "2026-10-14 16:50Z",
        })), utc("2026-10-14T22:50:00+00:00"))
        self.assertEqual(expires(flight(
            departure={"scheduled_time_utc": "2026-10-14 09:00Z", "revised_time_utc": "2026-10-14 09:40Z"},
            arrival={"scheduled_time_utc": "garbage"},
        )), utc("2026-10-15T09:40:00+00:00"))
        self.assertEqual(expires(flight()), utc("2026-10-16T00:00:00+00:00"))
        self.assertLess(expires(flight(date="not-a-date")), NOW)
        self.assertLess(expires(flight(date="9999-12-31")), NOW)
        self.assertEqual(expires(flight(arrival={"scheduled_time_utc": "9999-12-31 23:00Z"})),
                         utc("2026-10-16T00:00:00+00:00"))

    def test_concurrent_create_returns_the_winning_link(self):
        self.add_flight()
        real = share_service.find_active_share
        winner = uuid4().hex
        calls = []

        def racing(session, user_id, flight_id):
            if not calls:
                calls.append(True)
                with Session(self.engine) as other:
                    other.add(FlightShare(
                        id=winner, token_hash=share_service.token_hash(spec_token(winner)),
                        user_id=user_id, flight_id=flight_id,
                    ))
                    other.commit()
                return None
            return real(session, user_id, flight_id)

        with patch.object(share_service, "find_active_share", side_effect=racing):
            body = self.share()
        self.assertEqual(body["url"], settings.FLIGHT_SHARE_BASE_URL + spec_token(winner))
        self.assertFalse(body["created"])
        self.assertEqual([row.id for row in self.rows()], [winner])

    def test_partial_unique_index_allows_history_but_one_active_link(self):
        self.add_flight()
        with Session(self.engine) as session:
            for share_id, revoked in (("a" * 32, 1), ("b" * 32, None)):
                session.add(FlightShare(id=share_id, token_hash=share_id, user_id=OWNER,
                                        flight_id=FLIGHT_ID, revoked_at_ms=revoked))
            session.commit()
            session.add(FlightShare(id="c" * 32, token_hash="c" * 32, user_id=OWNER, flight_id=FLIGHT_ID))
            with self.assertRaises(IntegrityError):
                session.commit()

    # ---------------- public endpoints ------------------

    def test_malformed_tokens_are_404_without_touching_the_database(self):
        statements = []
        event.listen(self.engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
        for token in ("short", "a" * 21, "a" * 23, "a" * 21 + "=", "a" * 21 + "%20", "a" * 21 + ".",
                      "%E2%80%A8" + "a" * 21, "a" * 21 + "%0A"):
            with self.subTest(token=token):
                page = self.page(token)
                self.assertEqual(page.status_code, 404)
                self.assertIn('data-state="not_found"', page.text)
                self.assert_public_headers(page)
                live = self.live(token)
                self.assertEqual((live.status_code, live.json()), (404, {"state": "not_found"}))
                self.assert_public_headers(live)
        self.assertEqual(statements, [])

    def test_unknown_and_revoked_tokens_are_404(self):
        self.add_flight()
        token = self.token(self.share()["url"])
        unknown = spec_token(uuid4().hex)
        self.client.delete(f"/flights/{FLIGHT_ID}/share", headers=self.headers())
        for candidate in (unknown, token):
            page = self.page(candidate)
            self.assertEqual(page.status_code, 404)
            self.assertIn("This link is no longer available", page.text)
            self.assertNotIn("BA178", page.text)
            self.assertEqual(self.live(candidate).status_code, 404)

    def test_expired_share_is_410_without_flight_data(self):
        self.add_flight()
        token = self.token(self.share()["url"])
        self.now.return_value = datetime(2026, 10, 14, 22, 5, tzinfo=timezone.utc)
        page = self.page(token)
        self.assertEqual(page.status_code, 410)
        self.assert_public_headers(page)
        self.assertIn('data-state="expired"', page.text)
        self.assertNotIn("maplibre", page.text)
        self.assertNotIn("BA178", page.text)
        self.assertEqual(json.loads(STATE_BLOCK.search(page.text).group(1)), {"state": "expired"})
        live = self.live(token)
        self.assertEqual((live.status_code, live.json()), (410, {"state": "expired"}))
        self.assert_public_headers(live)
        self.assertEqual(self.client.head(f"/public/share/{token}").status_code, 410)

    def test_active_page_headers_meta_and_embedded_state(self):
        self.add_flight()
        token = self.token(self.share()["url"])
        page = self.page(token)
        self.assertEqual(page.status_code, 200)
        self.assert_public_headers(page)
        self.assertTrue(page.headers["content-type"].startswith("text/html"))
        text = page.text
        self.assertIn('<html lang="en" dir="ltr">', text)
        self.assertIn("<title>BA178 · LHR → JFK · Sofly</title>", text)
        self.assertEqual(meta(text, "property", "og:title"), "Follow BA178 live · LHR → JFK")
        self.assertEqual(meta(text, "property", "og:description"),
                         "Departs 14 Oct · Live status, gates and position on Sofly")
        self.assertEqual(meta(text, "property", "og:image"), "https://sofly.to/share-assets/og-flight.v1.png")
        self.assertEqual(meta(text, "name", "twitter:card"), "summary_large_image")
        self.assertEqual(meta(text, "name", "robots"), "noindex,nofollow,noarchive")
        self.assertEqual(meta(text, "name", "apple-itunes-app"), "app-id=6758215295")
        self.assertEqual(meta(text, "name", "theme-color"), "#5955f5")
        self.assertIsNotNone(meta(text, "name", "description"))
        self.assertIsNone(meta(text, "property", "og:url"))
        self.assertNotIn('rel="canonical"', text)

        parsed = tags(text)
        scripts = [attrs for tag, attrs in parsed if tag == "script"]
        self.assertEqual(scripts, [
            {"type": "application/json", "id": "sofly-share-state"},
            {"src": "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/5.24.0/maplibre-gl.js",
             "integrity": MAPLIBRE_JS_INTEGRITY, "crossorigin": "anonymous", "defer": None},
            {"src": "/share-assets/track.v1.js", "defer": None},
        ])
        styles = [attrs for tag, attrs in parsed if tag == "link" and attrs.get("rel") == "stylesheet"]
        self.assertEqual(styles, [
            {"rel": "stylesheet", "href": "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/5.24.0/maplibre-gl.css",
             "integrity": MAPLIBRE_CSS_INTEGRITY, "crossorigin": "anonymous"},
            {"rel": "stylesheet", "href": "/share-assets/track.v1.css"},
        ])
        self.assertFalse(any("style" in attrs for _, attrs in parsed))
        self.assertNotIn("<style", text)
        self.assertFalse(any(name.startswith("on") for _, attrs in parsed for name in attrs))
        main = text[text.index('<main id="app"'):text.index("</main>")]
        for expected in ("BA178", "LHR → JFK", "En route", "<noscript>"):
            self.assertIn(expected, main)

        embedded = json.loads(STATE_BLOCK.search(text).group(1))
        live = self.live(token)
        self.assertEqual(live.status_code, 200)
        self.assert_public_headers(live)
        self.assertEqual(embedded, live.json())

        head = self.client.head(f"/public/share/{token}", headers={"User-Agent": CHROME})
        self.assertEqual(head.status_code, 200)
        self.assert_public_headers(head)

    def test_language_comes_from_accept_language(self):
        self.add_flight()
        token = self.token(self.share()["url"])
        self.assertIn('<html lang="ar" dir="rtl">', self.page(token, **{"Accept-Language": "ar-EG,ar;q=0.9"}).text)
        self.assertIn('<html lang="pt-BR" dir="ltr">', self.page(token, **{"Accept-Language": "pt-PT"}).text)
        for header, expected in (
            (None, "en"), ("", "en"), ("xx, de;q=0.4", "de"), ("fr;q=0, it;q=0.2", "it"),
            ("tr;q=0.5, es;q=0.8", "es"), ("*", "en"), ("de;q=nan", "en"), ("en-GB,en;q=0.9", "en"),
            ("ja, fr-CA;q=0.7, pt-BR;q=0.7", "fr"),
        ):
            with self.subTest(header=header):
                self.assertEqual(pick_language(header), expected)

    def test_live_json_exact_shape_and_no_private_fields(self):
        self.add_flight()
        url = self.share()["url"]
        token = self.token(url)
        state = self.live(token).json()
        self.assertEqual(set(state), TOP_KEYS)
        self.assertEqual(set(state["position"]), {"lat", "lon", "heading_deg", "kind"})
        self.assertEqual(set(state["flight"]), FLIGHT_KEYS)
        self.assertEqual(set(state["flight"]["airline"]), {"name", "iata", "icao"})
        self.assertEqual(set(state["flight"]["aircraft"]), {"model", "registration"})
        for leg in ("departure", "arrival"):
            self.assertEqual(set(state["flight"][leg]), LEG_KEYS)
            self.assertEqual(set(state["flight"][leg]["airport"]), AIRPORT_KEYS)
            self.assertEqual(set(state["flight"][leg]["best"]), TIME_KEYS | {"kind"})
            for kind in ("scheduled", "revised", "predicted", "actual"):
                value = state["flight"][leg][kind]
                self.assertTrue(value is None or set(value) == TIME_KEYS)
        [row] = self.rows()
        secrets = {token, url, row.token_hash, row.id, OWNER, "PRIVATE_SUBSCRIPTION", "400abc"}
        for path, key, value in walk(state):
            self.assertNotIn(key, FORBIDDEN_KEYS, path)
            self.assertNotEqual(value, FLIGHT_ID, path)
            if isinstance(value, str):
                self.assertFalse(any(secret in value for secret in secrets), path)

        self.assertEqual({key: state[key] for key in TOP_KEYS - {"position", "flight", "progress"}}, {
            "schema_version": 1, "state": "active", "server_time_utc": "2026-10-14T12:00:00Z",
            "expires_at_utc": "2026-10-14T22:05:00Z", "refresh_after_seconds": 30, "phase": "airborne",
        })
        flight = state["flight"]
        self.assertEqual(
            {key: flight[key] for key in ("number", "date", "status", "is_delayed", "distance_km")},
            {"number": "BA178", "date": "2026-10-14", "status": "EnRoute", "is_delayed": False, "distance_km": 5540},
        )
        self.assertEqual(flight["airline"], {"name": "British Airways", "iata": "BA", "icao": "BAW"})
        self.assertEqual(flight["aircraft"], {"model": "Boeing 777-300ER", "registration": "G-STBA"})
        departure, arrival = flight["departure"], flight["arrival"]
        self.assertEqual(departure["airport"], {
            "iata": "LHR", "name": "London Heathrow", "city": "London", "country_code": "GB",
            "lat": 51.4706, "lon": -0.461941,
        })
        self.assertEqual((departure["terminal"], departure["gate"], departure["checkin_desk"]), ("5", "B32", None))
        self.assertEqual(departure["scheduled"], {"utc": "2026-10-14T09:00:00Z", "local": "2026-10-14T10:00:00+01:00"})
        self.assertEqual(departure["actual"], {"utc": "2026-10-14T09:12:00Z", "local": "2026-10-14T10:12:00+01:00"})
        self.assertIsNone(departure["revised"])  # unparseable provider value
        self.assertEqual(departure["best"], {
            "kind": "actual", "utc": "2026-10-14T09:12:00Z", "local": "2026-10-14T10:12:00+01:00",
        })
        self.assertEqual(departure["delay_minutes"], 12)
        self.assertEqual(arrival["best"], {
            "kind": "predicted", "utc": "2026-10-14T16:05:00Z", "local": "2026-10-14T12:05:00-04:00",
        })
        self.assertEqual((arrival["delay_minutes"], arrival["baggage_belt"]), (-55, None))
        self.assertEqual(state["progress"], round(168 / 413, 4))

    def test_time_normalisation_handles_provider_iso_and_naive_values(self):
        pair = share_service._time_pair
        self.assertEqual(share_service._time_object(pair("2026-10-14 08:30Z", "2026-10-14 10:30+02:00")),
                         {"utc": "2026-10-14T08:30:00Z", "local": "2026-10-14T10:30:00+02:00"})
        self.assertEqual(share_service._time_object(pair("2026-10-14T08:30:45.120Z", "2026-10-14T10:30:45")),
                         {"utc": "2026-10-14T08:30:45Z", "local": "2026-10-14T10:30:45+02:00"})
        self.assertEqual(share_service._time_object(pair(None, "2026-10-14 03:00-05:30")),
                         {"utc": "2026-10-14T08:30:00Z", "local": "2026-10-14T03:00:00-05:30"})
        self.assertEqual(share_service._time_object(pair("2026-10-14 08:30Z", "garbage")),
                         {"utc": "2026-10-14T08:30:00Z", "local": None})
        for utc, local in ((None, None), ("garbage", None), ("", "2026-10-14 10:30"), ("x" * 100, None),
                           (None, "0001-01-01 00:30+14:00"), ("9999-12-31 23:00Z", None)):
            self.assertIsNone(pair(utc, local))
        # Out-of-range provider years are missing data, never overflow errors.
        self.assertEqual(share_service._time_object(pair("2026-10-14 08:30Z", "0001-01-01 00:00")),
                         {"utc": "2026-10-14T08:30:00Z", "local": None})

    def test_phase_progress_and_position_math(self):
        lat, lon, heading = share_service.great_circle_point((0.0, 10.0), (0.0, 20.0), 0.5)
        self.assertAlmostEqual(lat, 0.0)
        self.assertAlmostEqual(lon, 15.0)
        self.assertAlmostEqual(heading, 90.0)
        lat, lon, heading = share_service.great_circle_point((10.0, 5.0), (20.0, 5.0), 0.5)
        self.assertAlmostEqual(lat, 15.0)
        self.assertAlmostEqual(lon, 5.0)
        self.assertAlmostEqual(min(heading, 360.0 - heading), 0.0)
        self.assertIsNone(share_service.great_circle_point((10.0, 5.0), (10.0, 5.0), 0.5))
        self.assertIsNone(share_service.great_circle_point((0.0, 0.0), (0.0, 180.0), 0.5))

        # Independent geometry checks on a real route: the point lies on the
        # route plane at fraction f of its arc, and heading follows the path.
        def vector(point):
            phi, lam = map(math.radians, point)
            return (math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi))

        def angle(a, b):
            return math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(vector(a), vector(b))))))

        london, new_york = (51.4706, -0.461941), (40.6398, -73.7789)
        normal = [vector(london)[(i + 1) % 3] * vector(new_york)[(i + 2) % 3]
                  - vector(london)[(i + 2) % 3] * vector(new_york)[(i + 1) % 3] for i in range(3)]
        for fraction in (0.1, 0.4068, 0.9):
            lat, lon, heading = share_service.great_circle_point(london, new_york, fraction)
            self.assertAlmostEqual(sum(a * b for a, b in zip(normal, vector((lat, lon)))), 0.0, places=9)
            self.assertAlmostEqual(angle(london, (lat, lon)), fraction * angle(london, new_york), places=9)
            ahead = share_service.great_circle_point(london, new_york, fraction + 1e-6)
            step = math.degrees(math.atan2(
                math.sin(math.radians(ahead[1] - lon)) * math.cos(math.radians(ahead[0])),
                math.cos(math.radians(lat)) * math.sin(math.radians(ahead[0]))
                - math.sin(math.radians(lat)) * math.cos(math.radians(ahead[0])) * math.cos(math.radians(ahead[1] - lon)),
            )) % 360
            self.assertAlmostEqual(heading, step, places=3)
            self.assertTrue(225 < heading < 290)

        self.add_flight()
        token = self.token(self.share()["url"])
        cases = (
            (datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc), "pre_departure", None, False),
            (NOW, "airborne", round(168 / 413, 4), True),
            (datetime(2026, 10, 14, 16, 5, tzinfo=timezone.utc), "landed", 1.0, False),
        )
        for now, phase, progress, has_position in cases:
            with self.subTest(phase=phase):
                self.now.return_value = now
                with Session(self.engine) as session:
                    flight = session.get(Flight, FLIGHT_ID)
                    flight.status = "Expected" if phase == "pre_departure" else "EnRoute"
                    session.add(flight)
                    session.commit()
                state = self.live(token).json()
                self.assertEqual((state["phase"], state["progress"]), (phase, progress))
                self.assertEqual(state["position"] is not None, has_position)
        self.now.return_value = NOW
        position = self.live(token).json()["position"]
        expected = share_service.great_circle_point((51.4706, -0.461941), (40.6398, -73.7789), round(168 / 413, 4))
        self.assertEqual(position, {
            "lat": round(expected[0], 5), "lon": round(expected[1], 5),
            "heading_deg": round(expected[2], 1), "kind": "estimated",
        })

        phase = share_service.flight_phase
        dep, arr = NOW - timedelta(hours=2), NOW + timedelta(hours=3)
        self.assertEqual(phase("Canceled", dep, NOW - timedelta(minutes=1), NOW), "canceled")
        self.assertEqual(phase("CanceledUncertain", dep, arr, NOW), "canceled")
        self.assertEqual(phase("Diverted", dep, NOW - timedelta(hours=1), NOW), "diverted")
        self.assertEqual(phase("Arrived", dep, arr, NOW), "landed")
        self.assertEqual(phase("Departed", NOW + timedelta(minutes=5), arr, NOW), "airborne")
        self.assertEqual(phase("Expected", dep, arr, NOW), "airborne")
        self.assertEqual(phase("Boarding", None, None, NOW), "pre_departure")
        self.assertEqual(phase("Unknown", None, None, NOW), "unknown")
        self.assertEqual(share_service.flight_progress("airborne", NOW + timedelta(minutes=5), arr, NOW), 0.0)
        self.assertIsNone(share_service.flight_progress("airborne", dep, None, NOW))
        self.assertIsNone(share_service.flight_progress("canceled", dep, arr, NOW))

    def test_canceled_and_delayed_flights_report_status_without_position(self):
        self.add_flight(status="Canceled")
        token = self.token(self.share()["url"])
        state = self.live(token).json()
        self.assertEqual((state["phase"], state["progress"], state["position"]), ("canceled", None, None))
        self.assertIn("Canceled", self.page(token).text)

        delayed = {"scheduled_time_utc": "2026-10-14 13:00Z", "revised_time_utc": "2026-10-14 13:20Z"}
        self.add_flight(flight_id=FLIGHT_ID + 1, status="Expected", departure=delayed, arrival={})
        state = self.live(self.token(self.share(FLIGHT_ID + 1)["url"])).json()
        self.assertEqual((state["phase"], state["flight"]["is_delayed"]), ("pre_departure", True))
        self.assertEqual(state["flight"]["departure"]["delay_minutes"], 20)
        self.assertIsNone(state["flight"]["arrival"]["best"])

        self.add_flight(flight_id=FLIGHT_ID + 2, status="Delayed", departure={}, arrival={})
        state = self.live(self.token(self.share(FLIGHT_ID + 2)["url"])).json()
        self.assertTrue(state["flight"]["is_delayed"])
        self.assertEqual(state["expires_at_utc"], "2026-10-16T00:00:00Z")

    def test_placeholder_airports_have_null_coordinates_and_no_position(self):
        self.add_flight(
            departure_airport=Airport(iata="LHR", name="London Heathrow", municipality_name="London",
                                      lat=0.0, lon=0.0, country_code="GB"),
            arrival_airport=Airport(iata="JFK", name="unknown", municipality_name="",
                                    lat=40.6, lon=-73.7, country_code=""),
        )
        state = self.live(self.token(self.share()["url"])).json()
        self.assertEqual(state["phase"], "airborne")
        self.assertIsNone(state["position"])
        self.assertEqual(state["flight"]["departure"]["airport"], {
            "iata": "LHR", "name": "London Heathrow", "city": "London", "country_code": "GB",
            "lat": None, "lon": None,
        })
        self.assertEqual(state["flight"]["arrival"]["airport"], {
            "iata": "JFK", "name": None, "city": None, "country_code": None, "lat": None, "lon": None,
        })

    # ---------------- revocation hooks ------------------

    def test_flight_delete_revokes_share(self):
        self.add_flight()
        token = self.token(self.share()["url"])
        response = self.client.delete(f"/flights/{FLIGHT_ID}/delete", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        [row] = self.rows()
        self.assertEqual(row.revoke_reason, "flight_removed")
        self.assertIsNotNone(row.revoked_at_ms)
        self.assertEqual(self.page(token).status_code, 404)
        self.assertEqual(self.live(token).status_code, 404)
        self.assertEqual(self.client.delete(f"/flights/{FLIGHT_ID}/share", headers=self.headers()).status_code, 404)

    def test_untracked_flight_is_not_public_even_without_a_hook(self):
        self.add_flight()
        token = self.token(self.share()["url"])
        with Session(self.engine) as session:
            session.delete(session.get(UserFlightLink, (OWNER, FLIGHT_ID)))
            session.commit()
        self.assertEqual(self.page(token).status_code, 404)

    def test_account_delete_revokes_every_share(self):
        self.add_flight()
        self.add_flight(flight_id=FLIGHT_ID + 1)
        self.add_flight(flight_id=FLIGHT_ID + 2, owner=OTHER)
        self.share(FLIGHT_ID)
        self.client.delete(f"/flights/{FLIGHT_ID}/share", headers=self.headers())  # earlier history row
        tokens = [self.token(self.share(flight_id)["url"]) for flight_id in (FLIGHT_ID, FLIGHT_ID + 1)]
        other = self.token(self.share(FLIGHT_ID + 2, owner=OTHER)["url"])
        self.assertEqual(self.client.delete("/users/me/", headers=self.headers()).status_code, 200)
        rows = self.rows()
        # No row keeps the deleted account's id next to the flights it tracked.
        self.assertNotIn(OWNER, {row.user_id for row in rows})
        deleted = [row for row in rows if row.user_id == share_service.DELETED_ACCOUNT_OWNER]
        self.assertEqual(sorted(row.flight_id for row in deleted), [FLIGHT_ID, FLIGHT_ID, FLIGHT_ID + 1])
        self.assertTrue(all(row.revoked_at_ms is not None for row in deleted))
        self.assertEqual(sorted(row.revoke_reason for row in deleted), ["account_deleted", "account_deleted", "owner"])
        [kept] = [row for row in rows if row.user_id == OTHER]
        self.assertIsNone(kept.revoked_at_ms)
        for token in tokens:
            self.assertEqual(self.page(token).status_code, 404)
        self.assertEqual(self.page(other).status_code, 200)

    # ---------------- views, providers and logs ------------------

    def test_view_counting_skips_bots_head_and_polls(self):
        self.add_flight()
        token = self.token(self.share()["url"])
        self.assertEqual(self.page(token).status_code, 200)
        self.assertEqual(self.page(token, **{"User-Agent": "Mozilla/5.0 (Linux; Android 15) Chrome/130"}).status_code, 200)
        for agent in BOT_AGENTS:
            with self.subTest(agent=agent):
                self.assertEqual(self.client.get(f"/public/share/{token}", headers={"User-Agent": agent}).status_code, 200)
        self.assertEqual(self.client.head(f"/public/share/{token}", headers={"User-Agent": CHROME}).status_code, 200)
        for name, value in (("Sec-Purpose", "prefetch"), ("Sec-Purpose", "prefetch;prerender"),
                            ("Purpose", "prefetch"), ("X-Purpose", "preview"), ("X-Moz", "prefetch")):
            with self.subTest(prefetch=name):
                self.assertEqual(self.page(token, **{name: value}).status_code, 200)
        self.assertEqual(self.live(token).status_code, 200)
        self.assertEqual(self.client.head(f"/public/share/{token}/live.json").status_code, 200)
        [row] = self.rows()
        self.assertEqual(row.view_count, 2)
        self.assertIsNotNone(row.last_viewed_at_ms)

    def test_share_paths_never_call_flight_data_providers(self):
        with ExitStack() as stack:
            for client, methods in (
                (AerodataboxClient, ("__init__", "get_flight", "get_airport_flights", "get_flight_delays",
                                     "get_airport_delay")),
                (ADSBExchangeClient, ("__init__", "get_global_positions", "get_positions_for_path",
                                      "get_positions_for_registration")),
                (OpenSkyClient, ("__init__", "get_global_positions")),
            ):
                for method in methods:
                    replacement = AsyncMock if method != "__init__" else None
                    stack.enter_context(patch.object(
                        client, method, side_effect=AssertionError("provider called"),
                        **({"new_callable": replacement} if replacement else {}),
                    ))
            stack.enter_context(patch.object(
                httpx.AsyncClient, "send", side_effect=AssertionError("network called"),
            ))
            self.add_flight()
            token = self.token(self.share()["url"])
            self.assertEqual(self.client.get(f"/flights/{FLIGHT_ID}/share", headers=self.headers()).status_code, 200)
            for now in (datetime(2026, 10, 14, 8, tzinfo=timezone.utc), NOW, datetime(2026, 10, 14, 17, tzinfo=timezone.utc)):
                self.now.return_value = now
                self.assertEqual(self.page(token).status_code, 200)
                self.assertEqual(self.client.head(f"/public/share/{token}").status_code, 200)
                self.assertEqual(self.live(token).status_code, 200)
            self.now.return_value = datetime(2026, 10, 15, tzinfo=timezone.utc)
            self.assertEqual(self.page(token).status_code, 410)
            self.assertEqual(self.client.delete(f"/flights/{FLIGHT_ID}/share", headers=self.headers()).status_code, 200)
            self.assertEqual(self.page(token).status_code, 404)

    def test_token_url_hash_and_identifiers_never_reach_logs(self):
        self.add_flight()
        # INFO, not DEBUG: SQLAlchemy would otherwise log raw result rows for every
        # table, which production never enables. Parameters stay hidden at INFO.
        with self.assertLogs(level="INFO") as logs:
            url = self.share()["url"]
            token = self.token(url)
            self.share()
            self.client.get(f"/flights/{FLIGHT_ID}/share", headers=self.headers())
            self.page(token)
            self.client.head(f"/public/share/{token}")
            self.live(token)
            self.page(spec_token(uuid4().hex))
            self.live("bad")
            with patch.object(share_service, "build_live_state", side_effect=RuntimeError(f"boom {token} {OWNER}")):
                failed_page = self.page(token)
                failed_live = self.live(token)
            with patch.object(share_service, "record_view", side_effect=RuntimeError(f"boom {url}")):
                self.assertEqual(self.page(token).status_code, 200)
            with patch.object(share_service, "create_or_reuse_share", side_effect=RuntimeError(f"boom {url}")):
                failed_create = self.client.post(f"/flights/{FLIGHT_ID}/share", headers=self.headers())
            with patch.object(share_service, "revoke_flight_shares", side_effect=RuntimeError(f"boom {url}")):
                failed_revoke = self.client.delete(f"/flights/{FLIGHT_ID}/share", headers=self.headers())
            self.client.delete(f"/flights/{FLIGHT_ID}/share", headers=self.headers())
            self.page(token)
            self.now.return_value = datetime(2026, 10, 15, tzinfo=timezone.utc)
            ended = self.client.post(f"/flights/{FLIGHT_ID}/share", headers=self.headers())
        self.assertEqual(ended.status_code, 409)
        self.assertEqual((failed_page.status_code, failed_live.status_code), (500, 500))
        self.assertEqual(failed_live.json(), {"state": "error"})
        self.assert_public_headers(failed_page)
        self.assertEqual((failed_create.status_code, failed_revoke.status_code), (500, 500))
        [row] = self.rows()
        secrets = (token, url, row.token_hash, row.id, OWNER, str(FLIGHT_ID))
        for response in (failed_page, failed_live, failed_create, failed_revoke):
            self.assertFalse(any(secret in response.text for secret in secrets))
        server_records = [
            # The test client's own outbound request log is not server logging.
            record for record in logs.records if not record.name.startswith(("httpx", "httpcore"))
        ]
        self.assertTrue(any(record.name == flight_share.__name__ for record in server_records))
        for record in server_records:
            rendered = logging.Formatter("%(name)s %(levelname)s %(message)s").format(record)
            for secret in secrets:
                self.assertNotIn(secret, rendered)
                self.assertNotIn(secret, repr(record.__dict__))
            self.assertFalse(record.exc_info)

    def test_server_access_log_lines_never_carry_the_token(self):
        token = spec_token(uuid4().hex)
        access_filter = ShareTokenLogFilter()
        for path in (f"/public/share/{token}", f"/public/share/{token}/live.json", f"/public/share/{token}?x=1"):
            with self.subTest(path=path):
                # uvicorn.protocols.http access line, then gunicorn's atom mapping.
                record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                                           '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:5000", "GET", path, "1.1", 200), None)
                self.assertTrue(access_filter.filter(record))
                self.assertNotIn(token, record.getMessage())
                self.assertIn("/public/share/[redacted]", record.getMessage())
                record = logging.LogRecord("gunicorn.access", logging.INFO, __file__, 1, "%(r)s %(U)s", None, None)
                record.args = {"r": f"GET {path} HTTP/1.1", "U": path, "s": "200"}
                access_filter.filter(record)
                self.assertNotIn(token, record.getMessage())
                record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, f"GET {path}", None, None)
                access_filter.filter(record)
                self.assertNotIn(token, record.getMessage())
        untouched = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", ("/flights/1/share",), None)
        access_filter.filter(untouched)
        self.assertEqual(untouched.getMessage(), "/flights/1/share")
        for name in ("uvicorn.access", "gunicorn.access"):
            self.assertTrue(any(isinstance(item, ShareTokenLogFilter) for item in logging.getLogger(name).filters))

    def test_owner_get_and_post_return_a_working_url_after_secret_rotation(self):
        self.add_flight()
        old = self.token(self.share()["url"])
        with patch.object(settings, "JWT_SECRET", settings.JWT_SECRET + "-rotated"):
            fetched = self.client.get(f"/flights/{FLIGHT_ID}/share", headers=self.headers())
            self.assertEqual(fetched.status_code, 200)
            self.assertFalse(fetched.json()["created"])
            new = self.token(fetched.json()["url"])
            self.assertNotEqual(new, old)
            self.assertEqual(self.page(new).status_code, 200)
            self.assertEqual(self.share()["url"], fetched.json()["url"])
        self.assertEqual(len(self.rows()), 1)

    def test_hostile_names_are_escaped_in_html_and_state_block(self):
        hostile_airline = '</script><script>alert("airline")</script>'
        hostile_name = "<img src=x onerror=alert(1)>&amp;"
        hostile_city = "Line Break  & <b>bold</b>"
        hostile_iata = '"><svg onload=alert(1)>'
        self.add_flight(
            airline_name=hostile_airline,
            departure_airport=Airport(iata=hostile_iata, name=hostile_name, municipality_name=hostile_city,
                                      lat=51.47, lon=-0.45, country_code="GB"),
        )
        token = self.token(self.share()["url"])
        page = self.page(token).text
        for raw in (hostile_airline, hostile_name, hostile_city, hostile_iata, "<script>alert", "<svg", "<img", "<b>"):
            self.assertNotIn(raw, page)
        self.assertIn(f"<title>BA178 · {html.escape(hostile_iata)} → JFK · Sofly</title>", page)
        self.assertEqual(page.count("<script"), 3)
        self.assertEqual({tag for tag, _ in tags(page)}, {
            "html", "head", "meta", "title", "link", "script", "body", "main", "section", "h1", "p", "noscript",
        })
        block = STATE_BLOCK.search(page).group(1)
        for character in ("<", ">", "&", " ", " "):
            self.assertNotIn(character, block)
        state = json.loads(block)
        self.assertEqual(state["flight"]["airline"]["name"], hostile_airline)
        self.assertEqual(state["flight"]["departure"]["airport"]["name"], hostile_name)
        self.assertEqual(state["flight"]["departure"]["airport"]["city"], hostile_city)
        self.assertEqual(state["flight"]["departure"]["airport"]["iata"], hostile_iata)
        self.assertEqual(state, self.live(token).json())

    # ---------------- contracts shared with other components ------------------

    def test_flight_share_analytics_values_are_fixed_codes(self):
        def event(schema=26, **properties):
            return DiagnosticEvent(
                event_id=uuid4(), event_name="flight_share_action", occurred_at_ms=1_790_000_000_000,
                installation_id=uuid4(), app_version="3.9", build_number="140",
                analytics_environment="development", build_configuration="debug",
                properties={"event_schema_version": schema, **properties},
            )

        base = {"action": "create", "outcome": "started", "source": "flight_detail"}
        for action in ("create", "copy", "share_sheet", "revoke"):
            for outcome in ("started", "succeeded", "failed", "presented", "completed", "cancelled"):
                event(**{**base, "action": action, "outcome": outcome})
        for reason in ("not_found", "ended", "rate_limited", "unavailable", "account_changed", "invalid_response"):
            event(**{**base, "outcome": "failed", "reason": reason})
        accepted = event(**base, flight_id=FLIGHT_ID, experience_presentation_id=str(uuid4()))
        self.assertEqual(accepted.properties.flight_id, FLIGHT_ID)
        for key, value in (("action", "share_link"), ("outcome", "opened"), ("reason", "timeout"),
                           ("source", "profile"), ("reason", "https://sofly.to/f/" + "a" * 22)):
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                event(**{**base, key: value})
        for omitted in base:
            with self.subTest(omitted=omitted), self.assertRaises(ValidationError):
                event(**{key: value for key, value in base.items() if key != omitted})
        with self.assertRaises(ValidationError):
            event(schema=19, **base)
        with self.assertRaises(ValidationError):
            event(**base, url="https://sofly.to/f/" + "a" * 22)

    def test_security_logging_has_share_operations(self):
        self.assertEqual(
            {CredentialOperation.FLIGHT_SHARE_CREATE.value, CredentialOperation.FLIGHT_SHARE_REVOKE.value,
             CredentialOperation.FLIGHT_SHARE_READ.value},
            {"flight_share_create", "flight_share_revoke", "flight_share_read"},
        )


if __name__ == "__main__":
    unittest.main()
