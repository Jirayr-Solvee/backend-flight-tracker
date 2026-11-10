import unittest
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

    def test_rejects_other_aircraft_and_prior_leg(self):
        self.assertIsNone(self.lookup([dict(self.row, hex="4CA708")]))
        self.assertIsNone(self.lookup([dict(self.row, last_seen="2026-11-10T10:59:59Z")]))
        self.assertIsNone(self.lookup([dict(self.row, callsign="RYR1TN")]))

    def test_rejects_landed_wrong_carrier_missing_airport_and_ambiguity(self):
        for changes in ({"datetime_landed": "2026-11-10T10:58:00Z"},
                        {"flight": "TK1762"}, {"dest_icao": None}):
            self.assertIsNone(self.lookup([dict(self.row, **changes)]))
        self.assertIsNone(self.lookup([self.row, dict(self.row, fr24_id="another-leg")]))


class PrivateGlobePreviewTests(unittest.IsolatedAsyncioTestCase):
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
            patch.object(fr24_route, "lookup_route", AsyncMock(return_value=("LFOB", "GMFF"))) as lookup,
        ):
            for user_id, allowed in (("other", False), ("qa", True)):
                result = await fixture.flights.resolve_aircraft_tracking_preview(
                    position.icao24, SimpleNamespace(id=user_id), session, fast=True, exact_only=True
                )
                self.assertEqual(result.context.route_status, "available" if allowed else "unavailable")
                self.assertIsNone(result.tracking_flight)
                self.assertIsNone(result.nearby_route)
            lookup.assert_awaited_once_with(position, "synthetic", "FR")
        session.commit.assert_not_called()
