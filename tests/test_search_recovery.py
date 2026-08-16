import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch


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


from core.models.aerodatabox import FlightStatusEnum
from core.models.flight import (
    AirlineRead,
    AirportFlightAirportInfoRead,
    AirportFlightOriginAndDestinationInfoRead,
    AirportFlightRead,
    AirportRead,
    ArrivalRead,
    DepartureRead,
    FlightRead,
    GlobalFlightPositionRead,
)
from core.services.flight.api_client import (
    ADSBExchangeRateLimitedError,
    AerodataboxClient,
    AerodataboxUnavailableError,
)
from core.services.flight.service import FlightQueryHandler, FlightService
from core.services.gemini.service import GeminiService, ResolvedFunctionCall


class SearchRecoveryTests(unittest.TestCase):
    def test_normalization_removes_rtl_marks(self):
        self.assertEqual(
            GeminiService.normalize_query("\u200f  من   المكسيك إلى تركيا  "),
            "من المكسيك إلى تركيا",
        )

    def test_aircraft_registration_gets_specific_recovery(self):
        recovery = GeminiService.preflight_recovery("N501KL")

        self.assertIsNotNone(recovery)
        self.assertEqual(recovery.reason, "aircraft_registration")
        self.assertEqual(recovery.normalized_query, "N501KL")

    def test_common_hyphenated_aircraft_registration_is_detected(self):
        self.assertEqual(
            GeminiService.aircraft_registration_from_query("Tail number G-STBA"),
            "G-STBA",
        )

    def test_arabic_country_route_returns_airport_choices(self):
        recovery = GeminiService.preflight_recovery("من المكسيك إلى تركيا")

        self.assertIsNotNone(recovery)
        self.assertEqual(recovery.reason, "choose_airport_route")
        self.assertEqual(
            [suggestion.label for suggestion in recovery.suggestions],
            ["MEX → IST", "CUN → IST", "MEX → SAW"],
        )

    def test_broad_country_returns_major_airports(self):
        recovery = GeminiService.preflight_recovery("Germany")

        self.assertIsNotNone(recovery)
        self.assertEqual(recovery.reason, "choose_airport")
        self.assertEqual(recovery.suggestions[0].query, "Departures from FRA Today")

    def test_rockport_returns_nearby_commercial_airport(self):
        recovery = GeminiService.preflight_recovery("Rockport Texas")

        self.assertIsNotNone(recovery)
        self.assertEqual(recovery.reason, "nearby_commercial_airport")
        self.assertEqual(recovery.suggestions[0].label, "CRP departures")

    def test_pasted_itinerary_extracts_hyphenated_flight_number(self):
        self.assertEqual(
            GeminiService._flight_from_query(
                "Opéré par V7 V7-2115 1h 35m Economy"
            ),
            ("V7", "2115"),
        )

    def test_explicit_english_date_is_not_replaced_with_today(self):
        self.assertEqual(
            GeminiService._date_from_query("fi528 august 17, 2026"),
            "2026-08-17",
        )

    def test_day_first_date_is_supported(self):
        self.assertEqual(
            GeminiService._date_from_query("fi528 17 august 2026"),
            "2026-08-17",
        )

    def test_iso_date_is_supported(self):
        self.assertEqual(
            GeminiService._date_from_query("fi528 2026-08-17"),
            "2026-08-17",
        )

    def test_named_dates_without_a_year_use_current_year(self):
        expected = f"{datetime.now(timezone.utc).year}-08-17"
        for query in (
            "fi528 August 17",
            "fi528 Aug 17",
            "fi528 17 August",
            "fi528 17 Aug",
            "fi528 Aug 17th",
            "fi528 17th Aug",
        ):
            with self.subTest(query=query):
                self.assertEqual(GeminiService._date_from_query(query), expected)

        self.assertEqual(
            GeminiService._date_from_query("fi528 January 1"),
            f"{datetime.now(timezone.utc).year}-01-01",
        )

    def test_numeric_day_first_dates_are_supported(self):
        expected = f"{datetime.now(timezone.utc).year}-08-17"
        for query in (
            "fi528 17 08",
            "fi528 17/08",
            "fi528 17-08",
            "fi528 17.08",
        ):
            with self.subTest(query=query):
                self.assertEqual(GeminiService._date_from_query(query), expected)

    def test_numeric_dates_accept_year_and_unambiguous_month_first(self):
        expected = {
            "fi528 17 08 2026": "2026-08-17",
            "fi528 17/08/26": "2026-08-17",
            "fi528 08/17/2026": "2026-08-17",
            "fi528 2026/08/17": "2026-08-17",
        }
        for query, date in expected.items():
            with self.subTest(query=query):
                self.assertEqual(GeminiService._date_from_query(query), date)

    def test_empty_flight_without_date_asks_for_date(self):
        resolved = ResolvedFunctionCall(
            function_name="extract_flight_info",
            args={
                "airline_iata": "FL",
                "flight_number": "528",
                "departure_date": "2026-08-16",
            },
            handler=lambda **_: None,
        )

        recovery = GeminiService.recovery_for_empty_result("FL528", resolved)

        self.assertEqual(recovery.reason, "missing_date")
        self.assertEqual(recovery.suggestions[0].kind, "add_date")

    def test_empty_flight_with_date_is_provider_no_match(self):
        resolved = ResolvedFunctionCall(
            function_name="extract_flight_info",
            args={
                "airline_iata": "FL",
                "flight_number": "528",
                "departure_date": "2026-08-17",
            },
            handler=lambda **_: None,
        )

        recovery = GeminiService.recovery_for_empty_result(
            "FL528 August 17, 2026",
            resolved,
        )

        self.assertEqual(recovery.reason, "flight_not_found")
        self.assertEqual(
            GeminiService.failure_reason_for_recovery(recovery),
            "provider_no_match",
        )

    def test_confirmed_code_confusion_still_gets_a_correction(self):
        resolved = ResolvedFunctionCall(
            function_name="extract_flight_info",
            args={
                "airline_iata": "EL",
                "flight_number": "822",
                "departure_date": "2026-08-16",
            },
            handler=lambda **_: None,
        )

        recovery = GeminiService.recovery_for_empty_result("EL822", resolved)

        self.assertEqual(recovery.reason, "possible_flight_number_typo")
        self.assertEqual(recovery.suggestions[0].query, "EK822")

    def test_corrected_flight_preserves_an_explicit_date(self):
        resolved = ResolvedFunctionCall(
            function_name="extract_flight_info",
            args={
                "airline_iata": "EL",
                "flight_number": "822",
                "departure_date": "2026-08-17",
            },
            handler=lambda **_: None,
        )

        recovery = GeminiService.recovery_for_empty_result(
            "EL822 17 August 2026",
            resolved,
        )

        self.assertEqual(recovery.suggestions[0].query, "EK822 2026-08-17")

    def test_unparsed_flight_shape_gets_structured_failure(self):
        recovery = GeminiService.recovery_for_empty_result("ZZ987", None)

        self.assertEqual(recovery.reason, "unrecognized_flight_number")
        self.assertEqual(
            GeminiService.failure_reason_for_recovery(recovery),
            "unrecognized_flight_number",
        )


class LiveRegistrationSearchTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _position(callsign: str | None = "DAL915") -> GlobalFlightPositionRead:
        return GlobalFlightPositionRead(
            id="a6a42f",
            icao24="a6a42f",
            callsign=callsign,
            display_code=callsign or "A6A42F",
            lat=34.0,
            lon=-118.0,
            on_ground=False,
        )

    @patch(
        "core.services.flight.service.ADSBExchangeClient.get_positions_for_registration",
        new_callable=AsyncMock,
    )
    async def test_registration_rate_limit_is_structured(
        self,
        get_positions: AsyncMock,
    ):
        get_positions.side_effect = ADSBExchangeRateLimitedError()

        result = await FlightService.resolve_live_aircraft_registration(
            session=MagicMock(),
            registration="N501KL",
        )

        self.assertEqual(result.failure_reason, "provider_rate_limited")

    @patch(
        "core.services.flight.service.ADSBExchangeClient.get_positions_for_registration",
        new_callable=AsyncMock,
    )
    async def test_registration_provider_failure_is_not_reported_as_not_live(
        self,
        get_positions: AsyncMock,
    ):
        get_positions.return_value = None

        result = await FlightService.resolve_live_aircraft_registration(
            session=MagicMock(),
            registration="N501KL",
        )

        self.assertEqual(result.failure_reason, "provider_unavailable")

    @patch(
        "core.services.flight.service.ADSBExchangeClient.get_positions_for_registration",
        new_callable=AsyncMock,
    )
    async def test_registration_not_live_is_distinct_from_provider_failure(
        self,
        get_positions: AsyncMock,
    ):
        get_positions.return_value = []

        result = await FlightService.resolve_live_aircraft_registration(
            session=MagicMock(),
            registration="N501KL",
        )

        self.assertEqual(result.failure_reason, "registration_not_live")
        self.assertEqual(result.provider_result_count, 0)

    @patch(
        "core.services.flight.service.FlightService.resolve_global_live_flight",
        new_callable=AsyncMock,
    )
    @patch(
        "core.services.flight.service.ADSBExchangeClient.get_positions_for_registration",
        new_callable=AsyncMock,
    )
    async def test_live_registration_resolves_scheduled_callsign(
        self,
        get_positions: AsyncMock,
        resolve_flight: AsyncMock,
    ):
        flight = MagicMock()
        get_positions.return_value = [self._position()]
        resolve_flight.return_value = flight

        result = await FlightService.resolve_live_aircraft_registration(
            session=MagicMock(),
            registration="N501KL",
        )

        self.assertIs(result.flight, flight)
        self.assertIsNone(result.failure_reason)
        resolve_flight.assert_awaited_once_with(
            session=ANY,
            callsign="DAL915",
            icao24="a6a42f",
        )


class ProviderAndRankingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _segment(
        *,
        iata: str,
        scheduled: str,
        arrival: bool,
    ) -> DepartureRead | ArrivalRead:
        values = {
            "terminal": None,
            "baggage_belt": None,
            "checkin_desk": None,
            "gate": None,
            "scheduled_time_local": scheduled,
            "scheduled_time_utc": scheduled,
            "revised_time_local": None,
            "revised_time_utc": None,
            "predicted_time_local": None,
            "predicted_time_utc": None,
            "runway_time_local": None,
            "runway_time_utc": None,
            "quality": [],
            "airport": AirportRead(
                name=iata,
                iata=iata,
                municipality_name=iata,
                lat=0,
                lon=0,
                country_code="US",
            ),
        }
        return ArrivalRead(**values) if arrival else DepartureRead(**values)

    @classmethod
    def _flight(
        cls,
        *,
        identifier: int,
        number: str,
        status: FlightStatusEnum,
        departure_time: str,
    ) -> FlightRead:
        return FlightRead(
            id=identifier,
            number=number,
            status=status,
            date="2026-08-17",
            distance_km=None,
            distance_mile=None,
            airline=AirlineRead(name="Airline", iata=number[:2], icao="TST"),
            departure=cls._segment(
                iata="LAX",
                scheduled=departure_time,
                arrival=False,
            ),
            arrival=cls._segment(
                iata="JFK",
                scheduled="2026-08-17 18:00Z",
                arrival=True,
            ),
        )

    @staticmethod
    def _airport_segment(
        *,
        iata: str,
        scheduled: str,
    ) -> AirportFlightOriginAndDestinationInfoRead:
        return AirportFlightOriginAndDestinationInfoRead(
            terminal=None,
            baggage_belt=None,
            checkin_desk=None,
            gate=None,
            scheduled_time_local=scheduled,
            scheduled_time_utc=scheduled,
            revised_time_local=None,
            revised_time_utc=None,
            predicted_time_local=None,
            predicted_time_utc=None,
            runway_time_local=None,
            runway_time_utc=None,
            quality=[],
            airport=AirportFlightAirportInfoRead(iata=iata),
        )

    def test_exact_number_wins_over_codeshare_for_same_physical_flight(self):
        codeshare = self._flight(
            identifier=1,
            number="AF9999",
            status=FlightStatusEnum.ENROUTE,
            departure_time="2026-08-17 12:00Z",
        )
        exact = self._flight(
            identifier=2,
            number="DL100",
            status=FlightStatusEnum.ENROUTE,
            departure_time="2026-08-17 12:00Z",
        )

        ranked = FlightQueryHandler._rank_exact_flights(
            [codeshare, exact],
            requested_number="DL100",
        )

        self.assertEqual([flight.number for flight in ranked], ["DL100"])

    def test_active_direct_result_sorts_before_landed_result(self):
        landed = self._flight(
            identifier=1,
            number="DL100",
            status=FlightStatusEnum.ARRIVED,
            departure_time="2026-08-16 12:00Z",
        )
        active = self._flight(
            identifier=2,
            number="DL100",
            status=FlightStatusEnum.ENROUTE,
            departure_time="2026-08-17 12:00Z",
        )

        ranked = FlightQueryHandler._rank_exact_flights(
            [landed, active],
            requested_number="DL100",
        )

        self.assertEqual([flight.status for flight in ranked], [
            FlightStatusEnum.ENROUTE,
            FlightStatusEnum.ARRIVED,
        ])

    def test_airport_codeshares_are_collapsed_and_active_version_wins(self):
        def airport_flight(number: str, status: FlightStatusEnum) -> AirportFlightRead:
            return AirportFlightRead(
                number=number,
                status=status,
                date="2026-08-17",
                airline=None,
                departure=self._airport_segment(
                    iata="LAX",
                    scheduled="2026-08-17 12:00Z",
                ),
                arrival=self._airport_segment(
                    iata="JFK",
                    scheduled="2026-08-17 18:00Z",
                ),
            )

        ranked = FlightQueryHandler._dedupe_airport_flights([
            airport_flight("AF9999", FlightStatusEnum.ARRIVED),
            airport_flight("DL100", FlightStatusEnum.ENROUTE),
        ])

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].number, "DL100")

    async def test_flight_provider_429_is_not_treated_as_no_match(self):
        client = AerodataboxClient()
        client.client.get = AsyncMock(return_value=MagicMock(status_code=429))
        try:
            with self.assertRaises(AerodataboxUnavailableError) as context:
                await client.get_flight("DL100", "2026-08-17")
        finally:
            await client.client.aclose()

        self.assertTrue(context.exception.rate_limited)


if __name__ == "__main__":
    unittest.main()
