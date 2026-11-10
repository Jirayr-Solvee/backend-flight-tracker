import unittest
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests import test_flight_share as fixture  # Initializes isolated backend settings.
from core.services.flight import FlightService, fr24_route
from core.services.flight.aircraft_context import AircraftContext, AircraftContextService
from core.services.flight.fr24_route import match_route


class FlightradarRouteTests(unittest.TestCase):
    def test_shared_burst_minute_limit_and_backoff(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(fr24_route.time, "time") as now:
            def reserve(worker):
                with patch.object(fr24_route, "_request_budget", worker):
                    return fr24_route._reserve_request()

            first = fr24_route.SchedulePool(directory)
            second = fr24_route.SchedulePool(directory)
            for offset in (0, 6, 12, 18, 24):
                now.return_value = 1000 + offset
                self.assertIsNone(reserve(first))
                self.assertIsNone(reserve(second))
                blocked = reserve(first)
                self.assertEqual(blocked, "request_burst_limit" if offset < 24 else "request_minute_limit")
                self.assertEqual(blocked.retry_after, 6 if offset < 24 else 36)
            now.return_value = 1060
            self.assertIsNone(reserve(second))
            with patch.object(fr24_route, "_request_budget", first):
                fr24_route._record_provider_backoff()
            self.assertEqual(reserve(second), "provider_backoff")
            now.return_value = 1121
            self.assertIsNone(reserve(second))

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
    async def test_preview_only_sends_retry_hint_when_route_is_missing(self):
        from fastapi import Response

        position = SimpleNamespace(icao24="4d227a", callsign="RYR1TM")
        with (patch.object(fixture.settings, "FR24_GLOBE_PREVIEW_TOKEN", "synthetic"),
              patch.object(fixture.settings, "FR24_GLOBE_PREVIEW_QA_USER_ID", "qa"),
              patch.object(FlightService, "_global_positions_cache", (None, [position])),
              patch.object(AircraftContextService, "get", AsyncMock()) as exact,
              patch.object(fr24_route, "lookup_route", AsyncMock(
                  return_value=(None, fr24_route.RetryReason("request_burst_limit", 2.2))))):
            for status in ("unavailable", "available"):
                exact.return_value = AircraftContext(icao24=position.icao24, route_status=status)
                response = Response()
                await fixture.flights.resolve_aircraft_tracking_preview(
                    position.icao24, SimpleNamespace(id="qa"), Mock(), fast=True, exact_only=True,
                    response=response
                )
                self.assertEqual(response.headers.get("X-Sofly-Route-Retry-After"),
                                 "3" if status == "unavailable" else None)

    async def test_missing_snapshot_uses_only_the_selected_aircraft(self):
        from core.services.flight.aircraft_context import SelectedAircraftPositions

        position = SimpleNamespace(icao24="4d227a", callsign="RYR1TM")
        session = Mock()
        with (patch.object(fixture.settings, "FR24_GLOBE_PREVIEW_TOKEN", "synthetic"),
              patch.object(fixture.settings, "FR24_GLOBE_PREVIEW_QA_USER_ID", "qa"),
              patch.object(FlightService, "_global_positions_cache", None),
              patch.object(FlightService, "_iata_for_airline_prefix", return_value="FR"),
              patch.object(AircraftContextService, "get", AsyncMock(
                  side_effect=lambda *a, **k: AircraftContext(icao24=position.icao24))),
              patch.object(SelectedAircraftPositions, "get", AsyncMock(return_value=position)) as selected,
              patch.object(fr24_route, "lookup_route", AsyncMock(
                  return_value=(("LFOB", "GMFF"), "matched"))) as lookup):
            result = await fixture.flights.resolve_aircraft_tracking_preview(
                position.icao24, SimpleNamespace(id="qa"), session, fast=True, exact_only=True
            )
            self.assertEqual(result.context.origin.code, "LFOB")
            self.assertIsNone(result.tracking_flight)
            selected.assert_awaited_once_with(position.icao24)
            lookup.assert_awaited_once_with(position, "synthetic", "FR")
            selected.return_value = SimpleNamespace(icao24="4ca708")
            result = await fixture.flights.resolve_aircraft_tracking_preview(
                position.icao24, SimpleNamespace(id="qa"), session, fast=True, exact_only=True
            )
            self.assertIsNone(result.context.origin)
            self.assertEqual(lookup.await_count, 1)

    async def test_private_preview_runs_both_lookups_in_parallel(self):
        import asyncio

        started = asyncio.Event()
        position = SimpleNamespace(icao24="4d227a", callsign="RYR1TM")
        async def exact(*args, **kwargs):
            await asyncio.wait_for(started.wait(), timeout=0.5)
            return AircraftContext(icao24=position.icao24)

        async def fallback(*args):
            started.set()
            return ("LFOB", "GMFF"), "matched"

        with (patch.object(fixture.settings, "FR24_GLOBE_PREVIEW_TOKEN", "synthetic"),
              patch.object(fixture.settings, "FR24_GLOBE_PREVIEW_QA_USER_ID", "qa"),
              patch.object(FlightService, "_global_positions_cache", (None, [position])),
              patch.object(AircraftContextService, "get", exact),
              patch.object(fr24_route, "lookup_route", fallback)):
            result = await fixture.flights.resolve_aircraft_tracking_preview(
                position.icao24, SimpleNamespace(id="qa"), Mock(), fast=True, exact_only=True
            )
        self.assertEqual(result.context.origin.code, "LFOB")
        self.assertIsNone(result.tracking_flight)
        self.assertNotIn("fast_preview_deadline", result.context._preview_reasons)

    async def test_nonstandard_callsign_uses_commercial_filters_and_exact_identity(self):
        observed = int(time.time()) - 2
        def iso(timestamp):
            return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        position = SimpleNamespace(icao24="4d227a", callsign="FR5223", on_ground=False,
                                   aircraft_categories=[], time_position=observed)
        row = dict(FlightradarRouteTests.row, reg="9H-QCE", operated_as="RYR",
                   first_seen=iso(observed - 10), last_seen=iso(observed + 1))
        client = AsyncMock()
        with (patch.object(fr24_route, "_reserve_request", return_value=None),
              patch.object(fr24_route, "_cached_route", return_value=None),
              patch.object(fr24_route, "_remember_route"),
              patch.object(fr24_route.httpx, "AsyncClient") as factory):
            factory.return_value.__aenter__.return_value = client
            for registration, provider_row, expected_filter, expected_route in (
                ("9H-QCE", row, {"registrations": "9H-QCE"}, ("LFOB", "GMFF")),
                ("9H-QCE", dict(row, hex="4CA708"), {"registrations": "9H-QCE"}, None),
                (None, dict(row, callsign="FR5223"), {"callsigns": "FR5223"}, ("LFOB", "GMFF")),
                (None, row, {"callsigns": "FR5223"}, None),
            ):
                position.registration = registration
                response = Mock(status_code=200)
                response.json.return_value = {"data": [provider_row]}
                client.get.return_value = response
                route, _ = await fr24_route.lookup_route(position, "synthetic", None)
                self.assertEqual(route, expected_route)
                params = client.get.await_args.kwargs["params"]
                self.assertEqual({k: params[k] for k in ("registrations", "callsigns") if k in params},
                                 expected_filter)
                self.assertEqual(params["categories"], "P,C")

    async def test_verified_repeat_is_shared_and_does_not_cross_observations(self):
        observed = int(time.time()) - 2
        position = SimpleNamespace(icao24="4d227a", callsign="RYR1TM", on_ground=False,
                                   aircraft_categories=[], time_position=observed)
        key = f"4d227a:RYR1TM:{observed}:FR"
        route = ("LFOB", "GMFF")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(fr24_route, "_verified_routes", fr24_route.SchedulePool(directory)):
                fr24_route._remember_route(key, route)
            with (patch.object(fr24_route, "_verified_routes", fr24_route.SchedulePool(directory)),
                  patch.object(fr24_route, "_reserve_request", return_value="request_burst_limit"),
                  patch.object(fr24_route.httpx, "AsyncClient") as client):
                self.assertEqual(await fr24_route.lookup_route(position, "synthetic", "FR"),
                                 (route, "matched_repeat"))
                position.time_position += 1
                self.assertEqual(await fr24_route.lookup_route(position, "synthetic", "FR"),
                                 (None, "request_burst_limit"))
                client.assert_not_called()

    async def test_cooldown_and_stale_positions_do_not_call_provider(self):
        position = SimpleNamespace(icao24="4d227a", callsign="RYR1TM", on_ground=False,
                                   aircraft_categories=[], time_position=int(time.time()))
        with (patch.object(fr24_route, "_reserve_request", return_value="request_burst_limit"),
              patch.object(fr24_route.httpx, "AsyncClient") as client):
            self.assertEqual(await fr24_route.lookup_route(position, "synthetic", "FR"),
                             (None, "request_burst_limit"))
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
