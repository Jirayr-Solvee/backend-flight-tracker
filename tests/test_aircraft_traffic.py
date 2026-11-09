"""Public synthetic aircraft. No network/provider, account or production writes."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_regional_traffic import fixture, position
from core.services.flight import FlightService
from core.services.flight.api_client import ADSBExchangeClient
from core.services.flight.aircraft_metadata import aircraft_metadata, visible_aircraft


class AircraftTrafficTests(unittest.TestCase):
    def test_provider_fields_and_flags(self):
        for flags in (3, "3"):
            metadata = aircraft_metadata(dict(dbFlags=flags, r=" N123AB ", t="C17", type="mlat", category="A7"))
            self.assertEqual(metadata["aircraft_categories"], ["military", "interesting", "helicopters"])
            self.assertEqual(metadata["registration"], "N123AB")
            self.assertEqual(metadata["aircraft_type"], "C17")
            self.assertEqual(metadata["position_source"], "mlat")
        for flags in (None, True, False, -1, 1.5, "bad", {}, []):
            self.assertEqual(aircraft_metadata(dict(dbFlags=flags))["aircraft_categories"], [])
        self.assertEqual(aircraft_metadata(dict(dbFlags=4 | 8))["aircraft_categories"], [], "Privacy flags are not military")

    def test_conservative_cargo_not_airframe_or_substring_guess(self):
        for code in ("FDX123", "UPS12A", "CLX1G", " fdx123 "):
            self.assertIn("cargo", aircraft_metadata(dict(flight=code))["aircraft_categories"])
        for code in ("FDX", "XFDX123", "FDX12-3", "AF123", "FDXXXX", "FX123"):
            self.assertNotIn("cargo", aircraft_metadata(dict(flight=code, t="B744"))["aircraft_categories"])

    def test_actual_parser_passes_metadata_without_inventing_it(self):
        row = ADSBExchangeClient._position_from_aircraft(
            dict(hex="abc123", lat=40, lon=44, flight="RCH123", r="sample", t="C17",
                 dbFlags=1, alt_baro=1000, gs=90, seen_pos=0, type="adsb_icao"), now_ms=1_000_000)
        self.assertEqual(row.registration, "sample")
        self.assertEqual(row.aircraft_categories, ["military"])
        unknown = ADSBExchangeClient._position_from_aircraft(dict(hex="abc124", lat=40, lon=44), now_ms=1_000_000)
        self.assertIsNone(unknown.registration)
        self.assertIsNone(unknown.aircraft_type)
        self.assertEqual(unknown.aircraft_categories, [])
        self.assertIsNone(unknown.time_position, "Missing position age must not become receipt time")
        for invalid in (float("nan"), float("inf"), "NaN", "Infinity"):
            self.assertIsNone(ADSBExchangeClient._float_value(invalid))
        for extra in (dict(category="C1"), dict(category="C2"), dict(type="adsb_icao_nt")):
            self.assertIsNone(ADSBExchangeClient._position_from_aircraft(
                dict(hex="abc124", lat=40, lon=44, **extra), now_ms=1_000_000))

    def test_broad_feed_keeps_low_slow_and_registration_only(self):
        now = 1000
        for changes in (dict(callsign=None), dict(callsign="N123AB"), dict(callsign="RYR84LH"),
                        dict(altitude_feet=300, ground_speed_kt=20)):
            self.assertTrue(visible_aircraft(position(1, time_position=now, **changes), now))
        for changes in (dict(on_ground=True), dict(time_position=600), dict(time_position=1031),
                        dict(time_position=None, last_contact=now), dict(lat=91), dict(lon=float("nan"))):
            values = dict(time_position=now)
            values.update(changes)
            self.assertFalse(visible_aircraft(position(1, **values), now))

    def test_opt_in_retains_categories_and_bounds_without_legacy_callsign_rules(self):
        from datetime import datetime, timezone
        now = int(datetime.now(timezone.utc).timestamp())
        rows = [position(i, time_position=now, aircraft_categories=["military"], callsign=None, altitude_feet=300, ground_speed_kt=20) for i in range(500)]
        rows += [position(1000 + i, time_position=now, aircraft_categories=["cargo"]) for i in range(500)]
        rows += [position(2000 + i, time_position=now) for i in range(5000)]
        with patch.object(FlightService, "_get_cached_global_positions", AsyncMock(return_value=rows)):
            result = asyncio.run(FlightService.get_global_flight_positions(70, aircraft=True))
            self.assertLessEqual(len(result), 70)
            self.assertEqual(len({row.icao24 for row in result}), len(result))
            self.assertTrue(any("military" in p.aircraft_categories for p in result))
            self.assertTrue(any("cargo" in p.aircraft_categories for p in result))
            self.assertLessEqual(len(asyncio.run(FlightService.get_global_flight_positions(20000, aircraft=True))), 5000)
            self.assertEqual(len(asyncio.run(FlightService.get_global_flight_positions(1, aircraft=True))), 1)

    def test_aircraft_opt_in_requires_auth_and_passes_flag(self):
        from types import SimpleNamespace
        app = fixture.FastAPI()
        app.include_router(fixture.flights.router, prefix="/flights")
        client = fixture.TestClient(app)
        self.assertIn(client.get("/flights/live-positions?aircraft=true").status_code, (401, 403))
        app.dependency_overrides[fixture.flights.get_current_user] = lambda: SimpleNamespace(id="synthetic")
        with patch.object(FlightService, "get_global_flight_positions", AsyncMock(return_value=[])) as fetch:
            self.assertEqual(client.get("/flights/live-positions?aircraft=true&regional=true&limit=5000").status_code, 200)
            fetch.assert_awaited_once_with(limit=5000, regional=True, aircraft=True)


if __name__ == "__main__":
    unittest.main()
