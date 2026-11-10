"""Synthetic public aircraft only. No provider/production requests or writes."""
import asyncio
import time
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, patch

import httpx
from tests.test_regional_traffic import fixture, position
from core.services.flight.aircraft_context import AircraftContext, AircraftContextService, ObservedAircraftTrails, SelectedAircraftPositions, parse_trace, matched_route
from core.services.flight.api_client import ADSBExchangeClient
from core.services.flight import FlightService


def row(seconds, lat=40, lon=44, flags=0, altitude=30000):
    return [seconds, lat, lon, altitude, 400, 90, flags]


class AircraftTraceTests(unittest.TestCase):
    def test_actual_trace_contract_and_identity(self):
        payload = dict(icao="abc123", timestamp=1000, trace=[row(0), row(60, lon=44.1), row(120, lon=44.2)])
        segments = parse_trace(payload, "abc123", 1120)
        self.assertEqual([p.timestamp for p in segments[0]], [1000, 1060, 1120])
        self.assertEqual(parse_trace(payload, "abc124", 1120), [])
        self.assertEqual(parse_trace(dict(payload, timestamp="nan"), "abc123", 1120), [])

    def test_gaps_stale_flags_duplicates_and_latest_leg(self):
        rows = [row(0), row(60), row(60), row(59, flags=2), row(120),
                row(180, flags=1), row(240), row(300), row(1000), row(1060)]
        segments = parse_trace(dict(icao="abc123", timestamp=1000, trace=rows), "abc123", 2100)
        self.assertEqual([[p.timestamp for p in s] for s in segments], [[1000, 1060, 1120], [1240, 1300], [2000, 2060]])
        rows += [row(1120, flags=2), row(1180)]
        segments = parse_trace(dict(icao="abc123", timestamp=1000, trace=rows), "abc123", 2200)
        self.assertEqual([[p.timestamp for p in s] for s in segments], [[2120, 2180]])

    def test_ground_invalid_jump_future_and_dateline(self):
        rows = [row(0), row(60), row(120, altitude="ground"), row(180), row(240)]
        segments = parse_trace(dict(icao="abc123", timestamp=1000, trace=rows), "abc123", 1240)
        self.assertEqual([p.timestamp for p in segments[0]], [1180, 1240])
        for bad in (row(120, lat=91), row(120, lon=float("nan")), [120], row(1000)):
            result = parse_trace(dict(icao="abc123", timestamp=1000, trace=[row(0), row(60), bad, row(180), row(240)]), "abc123", 1240)
            self.assertTrue(all(all(-90 <= p.lat <= 90 and -180 <= p.lon <= 180 for p in s) for s in result))
        segments = parse_trace(dict(icao="abc123", timestamp=1000, trace=[row(0, lon=179.9), row(60, lon=-179.9)]), "abc123", 1060)
        self.assertEqual(len(segments), 1, "Dateline is not a world-spanning jump")
        self.assertEqual(parse_trace(dict(icao="abc123", timestamp=1000, trace=[row(0), row(1, lat=-40, lon=-100)]), "abc123", 1060), [])

    def test_output_is_bounded_and_preserves_endpoints(self):
        rows = [row(i, lon=44 + i * .0001) for i in range(10000)]
        result = parse_trace(dict(icao="abc123", timestamp=1000, trace=rows), "abc123", 11000)
        self.assertEqual(len(result[0]), 128)
        self.assertEqual((result[0][0].timestamp, result[0][-1].timestamp), (1000, 10999))

    def test_route_requires_exact_identity_freshness_and_location(self):
        now = datetime.now(timezone.utc)
        airport = Obj(iata="LHR", icao="EGLL", name="Heathrow", municipalityName="London")
        flight = Obj(aircraft=Obj(modeS="ABC123", reg="SAMPLE"), status=Obj(value="EnRoute"),
                     lastUpdatedUtc=now.isoformat(), location=Obj(lat=40.15, lon=44.4, reportedAtUtc=now.isoformat()),
                     departure=Obj(airport=airport), arrival=Obj(airport=airport))
        aircraft = position(1, icao24="abc123", registration="SAMPLE")
        self.assertIsNotNone(matched_route(flight, aircraft, now))
        flight.aircraft.modeS = "abc124"
        self.assertIsNone(matched_route(flight, aircraft, now), "Contradictory modeS cannot fall back to same tail")
        flight.aircraft.modeS = None
        self.assertIsNotNone(matched_route(flight, aircraft, now))
        aircraft.registration = None
        self.assertIsNone(matched_route(flight, aircraft, now), "Callsign alone is not aircraft identity")
        flight.aircraft.modeS = "abc123"
        flight.location.lat = -40
        self.assertIsNone(matched_route(flight, aircraft, now))
        flight.location.lat = 40.15
        flight.lastUpdatedUtc = (now - timedelta(hours=1)).isoformat()
        self.assertIsNone(matched_route(flight, aircraft, now))
        flight.lastUpdatedUtc = now.isoformat()
        flight.status.value = "Arrived"
        self.assertIsNone(matched_route(flight, aircraft, now))


class AircraftContextAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ObservedAircraftTrails.aircraft.clear()
        AircraftContextService.cache.clear()
        AircraftContextService.pending.clear()
        AircraftContextService.slots = asyncio.Semaphore(4)
        AircraftContextService.trace_backoff_until = 0

    async def test_cache_coalescing_and_waiter_cancellation(self):
        now = int(datetime.now(timezone.utc).timestamp())
        aircraft = position(1, icao24="abc123", time_position=now)
        started, release = asyncio.Event(), asyncio.Event()
        async def trace(result, date):
            started.set(); await release.wait()
            result.trail_status = "available"
        with patch.object(FlightService, "_get_cached_global_positions", AsyncMock(return_value=[aircraft])), \
             patch.object(AircraftContextService, "trace", AsyncMock(side_effect=trace)) as fetch, \
             patch.object(AircraftContextService, "route", AsyncMock()):
            first = asyncio.create_task(AircraftContextService.get("abc123"))
            await started.wait()
            second = asyncio.create_task(AircraftContextService.get("abc123"))
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError): await first
            release.set()
            result = await second
            self.assertEqual(result.trail_status, "available")
            result.trail_status = "mutated"
            self.assertEqual((await AircraftContextService.get("abc123")).trail_status, "available")
            self.assertEqual(fetch.await_count, 1)
            self.assertFalse(AircraftContextService.pending)

    async def test_trace_transport_permissions_and_backoff(self):
        original = httpx.AsyncClient
        calls = []
        def handler(request):
            calls.append(str(request.url))
            self.assertEqual(request.headers["Accept-Encoding"], "gzip")
            return httpx.Response(403)
        with patch.object(fixture.settings, "ADSBEXCHANGE_API_KEY", "synthetic"), \
             patch.object(fixture.settings, "ADSBEXCHANGE_TRACE_ENABLED", True), \
             patch("core.services.flight.aircraft_context.httpx.AsyncClient", side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)):
            first = AircraftContext(icao24="abc123")
            await AircraftContextService.trace(first, datetime.now(timezone.utc))
            second = AircraftContext(icao24="abc124")
            await AircraftContextService.trace(second, datetime.now(timezone.utc))
        self.assertEqual(first.trail_status, "provider_unavailable")
        self.assertEqual(second.trail_status, "provider_unavailable")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith("/traces/23/trace_recent_abc123.json"))

    async def test_observed_fallback_and_history_is_opt_in(self):
        now = int(datetime.now(timezone.utc).timestamp())
        first = position(1, icao24="abc123", time_position=now-60)
        latest = position(1, icao24="abc123", time_position=now)
        ObservedAircraftTrails.record([first, latest])
        with patch.object(FlightService, "_get_cached_global_positions", AsyncMock(return_value=[latest])), \
             patch.object(fixture.settings, "ADSBEXCHANGE_TRACE_ENABLED", False), \
             patch.object(AircraftContextService, "route", AsyncMock()), \
             patch("core.services.flight.aircraft_context.httpx.AsyncClient") as network:
            result = await AircraftContextService.get("abc123")
        network.assert_not_called()
        self.assertEqual(result.trail_status, "partial")
        self.assertEqual(result.trail_source, "sofly_observed")
        self.assertEqual(len(result.segments[0]), 2)
        self.assertIsNone(result.origin)
        ObservedAircraftTrails.record([position(1, icao24="abc123", time_position=now+1, callsign="NEW123")])
        self.assertEqual(ObservedAircraftTrails.segments("abc123", first.callsign, now), [])

    async def test_observation_cache_bounds_and_stale_order(self):
        now = int(datetime.now(timezone.utc).timestamp())
        for offset in range(100):
            ObservedAircraftTrails.record([position(1, time_position=now-100+offset)])
        entry = ObservedAircraftTrails.aircraft["000001"][1]
        self.assertEqual(len(entry), 16)
        latest = entry[-1].timestamp
        ObservedAircraftTrails.record([position(1, time_position=latest-1)])
        self.assertEqual(entry[-1].timestamp, latest)
        ObservedAircraftTrails.record([position(i, time_position=now) for i in range(7000)])
        self.assertLessEqual(len(ObservedAircraftTrails.aircraft), 6000)


class AircraftContextRouteTests(unittest.TestCase):
    def test_selected_position_auth_and_validation(self):
        app = fixture.FastAPI()
        app.include_router(fixture.flights.router, prefix="/flights")
        client = fixture.TestClient(app)
        endpoint = "/flights/live-positions/"
        self.assertIn(client.get(endpoint + "abc123/position").status_code, (401, 403))
        app.dependency_overrides[fixture.flights.get_current_user] = lambda: Obj(id="synthetic")
        self.assertEqual(client.get(endpoint + "nothex/position").status_code, 422)
        with patch.object(SelectedAircraftPositions, "get", AsyncMock(return_value=None)) as lookup:
            self.assertIsNone(client.get(endpoint + "ABC123/position").json())
            lookup.assert_awaited_once_with("abc123")

    def test_selected_position_uses_hex_and_preserves_observation_time(self):
        now = int(time.time())
        item = dict(hex="000001", lat=40, lon=44, seen_pos=7)
        for payload in [dict(ac=[item], now=now), dict(item, now=now * 1000)]:
            self.assertEqual(ADSBExchangeClient._positions_from_payload(payload)[0].time_position, now - 7)
        fresh = position(1, time_position=now - 7)
        client = Obj(get_positions_for_path=AsyncMock(return_value=[
            position(1, time_position=now - 999), position(2, time_position=now), fresh]),
            client=Obj(aclose=AsyncMock()))
        with patch("core.services.flight.aircraft_context.ADSBExchangeClient", return_value=client), \
             patch.object(SelectedAircraftPositions, "cache", ObservedAircraftTrails.aircraft.copy()), \
             patch.object(SelectedAircraftPositions, "next_request_at", 0):
            self.assertEqual(asyncio.run(SelectedAircraftPositions.get("000001")), fresh)
            self.assertEqual(asyncio.run(SelectedAircraftPositions.get("000001")), fresh)
            client.get_positions_for_path.assert_awaited_once_with("hex/000001", raise_for_status=True)
            client.client.aclose.assert_awaited_once()

    def test_auth_validation_and_no_user_assignment(self):
        app = fixture.FastAPI()
        app.include_router(fixture.flights.router, prefix="/flights")
        client = fixture.TestClient(app)
        self.assertIn(client.get("/flights/live-positions/abc123/context").status_code, (401, 403))
        app.dependency_overrides[fixture.flights.get_current_user] = lambda: Obj(id="synthetic")
        self.assertEqual(client.get("/flights/live-positions/nothex/context").status_code, 422)
        with patch.object(AircraftContextService, "get", AsyncMock(return_value=AircraftContext(icao24="abc123"))) as lookup:
            self.assertEqual(client.get("/flights/live-positions/ABC123/context").status_code, 200)
            lookup.assert_awaited_once_with("abc123")


if __name__ == "__main__": unittest.main()
