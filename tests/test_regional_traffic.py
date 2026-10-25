"""Synthetic regional feed checks; no provider, credentials or production DB."""
import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Existing fixture isolates model initialization in a scratch database and
# configures synthetic credentials before importing the production modules.
from tests import test_flight_share as fixture
from core.services.flight import FlightService
from core.models.flight import GlobalFlightPositionRead


def position(index, *, lat=40.15, lon=44.4, **changes):
    values = dict(id=str(index), icao24=f"{index:06x}", callsign=f"AF{index + 100}",
                  display_code=f"AF{index + 100}", lat=lat, lon=lon,
                  on_ground=False, altitude_feet=30000, ground_speed_kt=400,
                  last_contact=int(datetime.now(timezone.utc).timestamp()))
    values.update(changes)
    return GlobalFlightPositionRead(**values)


class RegionalTrafficTests(unittest.TestCase):
    def test_regional_opt_in_and_legacy_bounds(self):
        rows = [position(i) for i in range(1500)]
        with patch.object(FlightService, "_get_cached_global_positions", AsyncMock(return_value=rows)), \
             patch.object(fixture.settings, "GLOBAL_FLIGHT_POSITIONS_RESPONSE_LIMIT", 70), \
             patch.object(fixture.settings, "GLOBAL_FLIGHT_POSITIONS_PER_CONTINENT_LIMIT", 10), \
             patch.object(fixture.settings, "GLOBAL_FLIGHT_MAJOR_AIRLINES_ONLY", False):
            self.assertEqual(len(asyncio.run(FlightService.get_global_flight_positions(5000))), 10)
            regional = asyncio.run(FlightService.get_global_flight_positions(20000, regional=True))
            self.assertEqual(len(regional), 1000)
            self.assertEqual(len({row.id for row in regional}), 1000)
            self.assertEqual(len(asyncio.run(FlightService.get_global_flight_positions(7, regional=True))), 7)

    def test_global_pool_is_bounded(self):
        coordinates = [(40, -100), (-20, -60), (48, 10), (0, 20), (40, 90), (-30, 140), (-70, 0)]
        rows = [position(group * 1100 + i, lat=lat, lon=lon)
                for group, (lat, lon) in enumerate(coordinates) for i in range(1100)]
        with patch.object(FlightService, "_get_cached_global_positions", AsyncMock(return_value=rows)), \
             patch.object(FlightService, "_global_position_is_on_route", return_value=True):
            self.assertEqual(len(asyncio.run(FlightService.get_global_flight_positions(20000, regional=True))), 5000)

    def test_regional_keeps_airborne_freshness_and_callsign_guards(self):
        good = position(1)
        bad = [position(2, on_ground=True), position(3, last_contact=1),
               position(4, altitude_feet=100), position(5, ground_speed_kt=10),
               position(6, callsign="RYR84LH", display_code="RYR84LH")]
        with patch.object(FlightService, "_get_cached_global_positions", AsyncMock(return_value=[good] + bad)):
            result = asyncio.run(FlightService.get_global_flight_positions(5000, regional=True))
        self.assertEqual([row.id for row in result], [good.id])

    def test_opt_in_does_not_apply_major_airline_preference(self):
        with patch.object(FlightService, "_get_cached_global_positions", AsyncMock(return_value=[position(1)])), \
             patch.object(FlightService, "_is_preferred_global_airline", return_value=False), \
             patch.object(fixture.settings, "GLOBAL_FLIGHT_MAJOR_AIRLINES_ONLY", True):
            self.assertEqual(asyncio.run(FlightService.get_global_flight_positions(5000)), [])
            self.assertEqual(len(asyncio.run(FlightService.get_global_flight_positions(5000, regional=True))), 1)

    def test_route_requires_auth_and_passes_opt_in(self):
        app = fixture.FastAPI()
        app.include_router(fixture.flights.router, prefix="/flights")
        client = fixture.TestClient(app)
        response = client.get("/flights/live-positions?regional=true")
        self.assertIn(response.status_code, (401, 403))
        app.dependency_overrides[fixture.flights.get_current_user] = lambda: SimpleNamespace(id="test")
        with patch.object(FlightService, "get_global_flight_positions", AsyncMock(return_value=[])) as fetch:
            self.assertEqual(client.get("/flights/live-positions?regional=true&limit=5000").status_code, 200)
            fetch.assert_awaited_once_with(limit=5000, regional=True)
            self.assertEqual(client.get("/flights/live-positions?regional=true&limit=20001").status_code, 422)


if __name__ == "__main__":
    unittest.main()
