import unittest
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests import test_flight_share as fixture  # Initializes isolated backend settings.
from core.services.flight import FlightService, fr24_route
from core.services.flight.aircraft_context import AircraftContext, AircraftContextService
from core.services.flight.fr24_route import match_route


class FlightradarRouteTests(unittest.TestCase):
    observed = datetime(2026, 11, 10, 11, tzinfo=timezone.utc).timestamp()
    row = {
        "fr24_id": "test-leg", "flight": "FR5223", "callsign": "RYR1TM", "hex": "4D227A",
        "orig_icao": "LFOB", "dest_icao": "GMFF",
        "first_seen": "2026-11-10T10:23:01Z", "last_seen": "2026-11-10T13:05:27Z",
    }

    def lookup(self, rows):
        return match_route(rows, "RYR1TM", "4d227a", self.observed, "FR")

    def test_accepts_one_aircraft_and_active_leg(self):
        self.assertEqual(self.lookup([self.row]), ("LFOB", "GMFF"))

    def test_unmapped_airline_needs_provider_operator_corroboration(self):
        def match(**changes):
            return match_route([dict(self.row, **changes)], "RYR1TM", "4d227a", self.observed, None)

        self.assertEqual(match(operated_as="RYR"), ("LFOB", "GMFF"))
        self.assertIsNone(match())
        self.assertIsNone(match(operated_as="THY"))
        self.assertIsNone(match(operated_as="RYR", hex="4CA708"))
        self.assertIsNone(match(operated_as="RYR", last_seen="2026-11-10T10:59:59Z"))

    def test_rejects_other_aircraft_and_prior_leg(self):
        self.assertIsNone(self.lookup([dict(self.row, hex="4CA708")]))
        self.assertIsNone(self.lookup([dict(self.row, last_seen="2026-11-10T10:59:59Z")]))
        self.assertIsNone(self.lookup([dict(self.row, callsign="RYR1TN")]))

    def test_rejects_landed_wrong_carrier_missing_airport_and_ambiguity(self):
        for changes in ({"datetime_landed": "2026-11-10T10:58:00Z"},
                        {"flight": "TK1762"}, {"dest_icao": None}):
            self.assertIsNone(self.lookup([dict(self.row, **changes)]))
        self.assertIsNone(self.lookup([self.row, dict(self.row, fr24_id="another-leg")]))

    def test_distinguishes_provider_rejection_reasons(self):
        def reason(rows):
            return fr24_route._match_route(rows, "RYR1TM", "4d227a", self.observed, "FR")[1]

        self.assertEqual(reason([]), "no_rows")
        self.assertEqual(reason([dict(self.row, hex="4CA708")]), "identity_mismatch")
        self.assertEqual(reason([dict(self.row, last_seen="2026-11-10T10:59:59Z")]), "observation_outside_leg")
        self.assertEqual(reason([dict(self.row, dest_icao=None)]), "invalid_airports")
        self.assertEqual(reason([self.row, dict(self.row, fr24_id="another-leg")]), "ambiguous_leg")


class PrivateGlobePreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_and_stale_positions_do_not_call_provider(self):
        position = SimpleNamespace(icao24="4d227a", callsign="RYR1TM", on_ground=False,
                                   aircraft_categories=[], time_position=int(time.time()))
        with (patch.object(fr24_route, "_next_request", time.monotonic() + 30),
              patch.object(fr24_route, "_cooldown_reason", "request_cooldown"),
              patch.object(fr24_route.httpx, "AsyncClient") as client):
            self.assertEqual(await fr24_route.lookup_route(position, "synthetic", "FR"),
                             (None, "request_cooldown"))
            position.time_position -= 600
            self.assertEqual(await fr24_route.lookup_route(position, "synthetic", "FR"),
                             (None, "observation_not_fresh"))
            client.assert_not_called()

    async def test_only_allowlisted_user_gets_display_only_fallback(self):
        position = SimpleNamespace(icao24="4d227a", callsign="RYR1TM")
        session = Mock()
        with (
            patch.object(fixture.settings, "FR24_GLOBE_PREVIEW_TOKEN", "synthetic"),
            patch.object(fixture.settings, "FR24_GLOBE_PREVIEW_QA_USER_ID", "qa"),
            patch.object(FlightService, "_global_positions_cache", (None, [position])),
            patch.object(FlightService, "_iata_for_airline_prefix", return_value="FR"),
            patch.object(AircraftContextService, "get", AsyncMock(
                side_effect=lambda *a, **k: AircraftContext(icao24=position.icao24)
            )),
            patch.object(fr24_route, "lookup_route", AsyncMock(return_value=(("LFOB", "GMFF"), "matched"))) as lookup,
            self.assertLogs(fixture.flights.logger, level="INFO") as logs,
        ):
            for user_id, allowed in (("other", False), ("qa", True)):
                result = await fixture.flights.resolve_aircraft_tracking_preview(
                    position.icao24, SimpleNamespace(id=user_id), session, fast=True, exact_only=True
                )
                self.assertEqual(result.context.route_status, "available" if allowed else "unavailable")
                self.assertIsNone(result.tracking_flight)
                self.assertIsNone(result.nearby_route)
            lookup.assert_awaited_once_with(position, "synthetic", "FR")
            primary = AircraftContext(icao24=position.icao24, route_status="available")
            with patch.object(AircraftContextService, "get", AsyncMock(return_value=primary)):
                result = await fixture.flights.resolve_aircraft_tracking_preview(
                    position.icao24, SimpleNamespace(id="qa"), session, fast=True, exact_only=True
                )
            self.assertEqual(lookup.await_count, 2)
            self.assertIsNone(result.context.origin)  # Existing primary context was not overwritten.
        self.assertEqual([line.split("reason=")[-1] for line in logs.output if "globe_fr24" in line],
                         ["matched", "matched"])
        session.commit.assert_not_called()
