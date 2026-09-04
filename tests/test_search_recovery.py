import os
import unittest
from datetime import datetime, timedelta, timezone
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
    QuerySearchResponse,
)
from core.routers.flights import _execute_search_with_date_fallback
from core.services.flight.api_client import (
    ADSBExchangeRateLimitedError,
    AerodataboxClient,
    AerodataboxUnavailableError,
)
from core.services.flight.service import FlightQueryHandler, FlightService
from core.services.gemini.service import GeminiService, ResolvedFunctionCall


class SearchRecoveryTests(unittest.TestCase):
    def test_flight_number_candidates_recover_digit_suffix_airline_codes(self):
        self.assertEqual(
            FlightService._flight_number_candidates(
                airline_iata="B6", flight_number="6524"
            ),
            ("6524", "524"),
        )
        self.assertEqual(
            FlightService._flight_number_candidates(
                airline_iata="U2", flight_number="21234"
            ),
            ("21234", "1234"),
        )

    def test_flight_number_candidates_recover_complete_designator(self):
        self.assertEqual(
            FlightService._flight_number_candidates(
                airline_iata="5J", flight_number="5J272"
            ),
            ("5J272", "272"),
        )

    def test_flight_number_candidates_preserve_normal_numbers(self):
        self.assertEqual(
            FlightService._flight_number_candidates(
                airline_iata="BA", flight_number="456"
            ),
            ("456",),
        )

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

    def test_numeric_only_queries_require_an_airline(self):
        for query in ("23", "4320", "4320 Jetzt"):
            with self.subTest(query=query):
                recovery = GeminiService.preflight_recovery(query)
                self.assertIsNotNone(recovery)
                self.assertEqual(recovery.reason, "missing_airline")
                self.assertEqual(
                    GeminiService.failure_reason_for_recovery(recovery),
                    "incomplete_query",
                )

        self.assertIsNone(GeminiService._iata_for_airline_token("43"))
        self.assertEqual(GeminiService._iata_for_airline_token("LX"), "LX")

    def test_observed_localized_single_cities_resolve_without_ai(self):
        service = GeminiService()
        expected = {
            "Libonne": "LIS",
            "Lisbonne": "LIS",
            "Lisbone": "LIS",
            "Abu Dhabi": "AUH",
            "Kuwait": "KWI",
            "Amritsar": "ATQ",
            "kannur": "CNN",
            "Düsseldorfer": "DUS",
            "Toronto": "YYZ",
            "Yellownknife": "YZF",
            "Halifax": "YHZ",
            "Zürich": "ZRH",
            "JED": "JED",
        }
        for query, airport_iata in expected.items():
            with self.subTest(query=query):
                resolved = service._deterministic_function_call(query)
                self.assertIsNotNone(resolved)
                self.assertEqual(
                    resolved.function_name,
                    "extract_flight_info_via_airport_single_derection",
                )
                self.assertEqual(resolved.args["airport_iata"], airport_iata)

    def test_observed_city_routes_resolve_without_ai(self):
        service = GeminiService()
        expected = {
            "Von Bukarest nach Memmingen": ("OTP", "FMM"),
            "Lyon Dubrovnik": ("LYS", "DBV"),
            "Marbella Oran Aujourdhui": ("AGP", "ORN"),
            "Dxb To Bali": ("DXB", "DPS"),
            "Hong Kong Milan Today": ("HKG", "MXP"),
            "Manila To Bahrain": ("MNL", "BAH"),
        }
        for query, route in expected.items():
            with self.subTest(query=query):
                resolved = service._deterministic_function_call(query)
                self.assertIsNotNone(resolved)
                self.assertEqual(
                    (
                        resolved.args["departure_airport_iata"],
                        resolved.args["arrival_airport_iata"],
                    ),
                    route,
                )

    def test_observed_broad_locations_return_airport_choices(self):
        georgia = GeminiService.preflight_recovery("جورجيا")
        indonesia = GeminiService.preflight_recovery("Indonesia to Riyadh")
        switzerland = GeminiService.preflight_recovery(
            "من سويسرا الى جورجيا"
        )

        self.assertEqual(georgia.reason, "choose_airport")
        self.assertEqual(georgia.suggestions[0].query, "Departures from TBS Today")
        self.assertEqual(indonesia.reason, "choose_airport_route")
        self.assertEqual(indonesia.suggestions[0].query, "CGK to RUH Today")
        self.assertEqual(switzerland.reason, "choose_airport_route")
        self.assertEqual(switzerland.suggestions[0].query, "ZRH to TBS Today")

    def test_aircraft_type_is_not_misclassified_as_a_flight(self):
        for query in ("FA7X", "Boeing 777-200", "Airbus A350-900"):
            with self.subTest(query=query):
                recovery = GeminiService.preflight_recovery(query)
                self.assertEqual(recovery.reason, "unsupported_aircraft_type")
                self.assertEqual(recovery.detected_query_type, "aircraft_type")

    def test_observed_flight_number_separators_and_airlines_parse(self):
        expected = {
            "j9.402": ("J9", "402"),
            "F3346": ("F3", "346"),
            "XC5052": ("XC", "5052"),
            "Ibo853": ("I2", "853"),
            "Con1778": ("DE", "1778"),
        }
        for query, flight in expected.items():
            with self.subTest(query=query):
                self.assertEqual(GeminiService._flight_from_query(query), flight)

    def test_country_plus_destination_returns_correct_airport_choices(self):
        recovery = GeminiService.preflight_recovery("Cipro Nuriberg")

        self.assertEqual(recovery.reason, "choose_airport_route")
        self.assertEqual(recovery.suggestions[0].query, "LCA to NUE Today")

    def test_airline_to_airport_query_calls_provider_instead_of_country_recovery(self):
        query = "Flair Canada A Cancun Mexico"

        self.assertIsNone(GeminiService.preflight_recovery(query))

        resolved = GeminiService()._deterministic_function_call(query)
        self.assertIsNotNone(resolved)
        self.assertEqual(
            resolved.function_name,
            "extract_flight_info_via_airport_single_derection",
        )
        self.assertEqual(resolved.args["airline_iata"], "F8")
        self.assertEqual(resolved.args["airport_iata"], "CUN")
        self.assertEqual(resolved.args["direction"], "Arrival")
        self.assertEqual(GeminiService.query_type_for_call(resolved), "airline_airport")

    def test_short_airport_alias_does_not_match_inside_airline_name(self):
        route = GeminiService._country_and_airport_route(
            "flair mexico",
            "mexico",
        )

        self.assertIsNone(route)

    def test_unparsed_query_is_not_labelled_as_provider_no_match(self):
        recovery = GeminiService.recovery_for_empty_result("Rev", None)

        self.assertEqual(recovery.reason, "no_results")
        self.assertEqual(
            GeminiService.failure_reason_for_recovery(recovery),
            "unsupported_or_incomplete_query",
        )

    def test_empty_route_after_provider_call_is_still_provider_no_match(self):
        resolved = ResolvedFunctionCall(
            function_name="extract_flight_info_via_airport",
            args={
                "departure_airport_iata": "OTP",
                "arrival_airport_iata": "FMM",
                "departure_date": "2026-08-20",
            },
            handler=lambda **_: None,
        )

        recovery = GeminiService.recovery_for_empty_result(
            "OTP to FMM Today",
            resolved,
        )

        self.assertEqual(recovery.reason, "route_not_found")
        self.assertEqual(
            GeminiService.failure_reason_for_recovery(recovery),
            "provider_no_match",
        )

    def test_observed_spanish_date_with_de_is_parsed(self):
        self.assertEqual(
            GeminiService._date_from_query("KR0152 18 De Agosto"),
            f"{datetime.now(timezone.utc).year}-08-18",
        )

    def test_upcoming_date_window_is_bounded_by_query_precision(self):
        self.assertEqual(GeminiService.upcoming_search_days("LX546"), 3)
        self.assertEqual(GeminiService.upcoming_search_days("LX546 Jetzt"), 1)
        self.assertEqual(
            GeminiService.upcoming_search_days("LX546 20 August 2026"),
            0,
        )

    def test_embedded_airport_codes_are_offered_as_route_recovery(self):
        service = GeminiService()
        resolved = service._deterministic_function_call("J9 530 ktmkwi")

        recovery = service.recovery_for_empty_result("J9 530 ktmkwi", resolved)

        self.assertEqual(recovery.suggestions[-1].query, "KTM to KWI Today")

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
        arrival_time = (
            datetime.strptime(departure_time, "%Y-%m-%d %H:%MZ")
            + timedelta(hours=6)
        ).strftime("%Y-%m-%d %H:%MZ")
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
                scheduled=arrival_time,
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

    @patch(
        "core.services.flight.service.AirportFlightMapper.airport_flight_to_airport_flight_read"
    )
    @patch(
        "core.services.flight.service.FlightService.get_airport_flights",
        new_callable=AsyncMock,
    )
    async def test_airline_airport_search_filters_other_airlines(
        self,
        get_airport_flights: AsyncMock,
        map_airport_flight: MagicMock,
    ):
        flair = MagicMock()
        flair.airline.iata = "F8"
        flair.number = "F8 1234"
        flair.departure.airport.iata = "YYZ"
        flair.arrival.airport.iata = "CUN"

        air_canada = MagicMock()
        air_canada.airline.iata = "AC"
        air_canada.number = "AC 1810"
        air_canada.departure.airport.iata = "YYZ"
        air_canada.arrival.airport.iata = "CUN"

        get_airport_flights.return_value = MagicMock(
            departures=[],
            arrivals=[flair, air_canada],
        )
        mapped = AirportFlightRead(
            number="F81234",
            status=FlightStatusEnum.EXPECTED,
            date="2026-09-03",
            airline=AirlineRead(name="Flair", iata="F8", icao="FLE"),
            departure=self._airport_segment(
                iata="YYZ",
                scheduled="2026-09-03 12:00Z",
            ),
            arrival=self._airport_segment(
                iata="CUN",
                scheduled="2026-09-03 16:00Z",
            ),
        )
        map_airport_flight.return_value = mapped

        response = await FlightQueryHandler.extract_flight_info_via_airport_single_derection(
            departure_date="2026-09-03",
            airport_iata="CUN",
            direction="Arrival",
            airline_iata="F8",
        )

        self.assertEqual([flight.number for flight in response.airport_flights_result], ["F81234"])
        map_airport_flight.assert_called_once()
        self.assertIs(map_airport_flight.call_args.kwargs["flight"], flair)

    async def test_flight_provider_429_is_not_treated_as_no_match(self):
        client = AerodataboxClient()
        client.client.get = AsyncMock(return_value=MagicMock(status_code=429))
        try:
            with self.assertRaises(AerodataboxUnavailableError) as context:
                await client.get_flight("DL100", "2026-08-17")
        finally:
            await client.client.aclose()

        self.assertTrue(context.exception.rate_limited)

    async def test_missing_date_searches_upcoming_days_until_a_result(self):
        requested_dates = []
        live_fallback_values = []

        async def handler(*, departure_date: str, session, **kwargs):
            requested_dates.append(departure_date)
            live_fallback_values.append(kwargs.get("allow_live_fallback"))
            if departure_date != "2026-08-22":
                return QuerySearchResponse()
            return QuerySearchResponse(
                flights_result=[
                    self._flight(
                        identifier=9,
                        number="LX546",
                        status=FlightStatusEnum.EXPECTED,
                        departure_time="2026-08-22 12:00Z",
                    )
                ]
            )

        resolved = ResolvedFunctionCall(
            function_name="extract_flight_info",
            args={
                "airline_iata": "LX",
                "flight_number": "546",
                "departure_date": "2026-08-20",
            },
            handler=handler,
        )

        response = await _execute_search_with_date_fallback(
            resolved_call=resolved,
            query="LX546",
            session=MagicMock(),
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )

        self.assertEqual(
            requested_dates,
            ["2026-08-20", "2026-08-21", "2026-08-22"],
        )
        self.assertEqual(response.flights_result[0].number, "LX546")
        self.assertEqual(resolved.args["departure_date"], "2026-08-22")
        self.assertEqual(live_fallback_values, [None, False, False])

    async def test_date_ambiguous_search_skips_landed_day_for_upcoming_flight(self):
        requested_dates = []

        async def handler(*, departure_date: str, session, **kwargs):
            requested_dates.append(departure_date)
            status = (
                FlightStatusEnum.ARRIVED
                if departure_date == "2026-08-20"
                else FlightStatusEnum.EXPECTED
            )
            return QuerySearchResponse(
                flights_result=[
                    self._flight(
                        identifier=len(requested_dates),
                        number="LX546",
                        status=status,
                        departure_time=f"{departure_date} 12:00Z",
                    )
                ]
            )

        resolved = ResolvedFunctionCall(
            function_name="extract_flight_info",
            args={
                "airline_iata": "LX",
                "flight_number": "546",
                "departure_date": "2026-08-20",
            },
            handler=handler,
        )

        response = await _execute_search_with_date_fallback(
            resolved_call=resolved,
            query="LX546",
            session=MagicMock(),
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )

        self.assertEqual(requested_dates, ["2026-08-20", "2026-08-21"])
        self.assertEqual(response.flights_result[0].status, FlightStatusEnum.EXPECTED)

    async def test_stale_expected_result_is_filtered_and_next_date_is_searched(self):
        requested_dates = []

        async def handler(*, departure_date: str, session, **kwargs):
            requested_dates.append(departure_date)
            return QuerySearchResponse(
                flights_result=[
                    self._flight(
                        identifier=len(requested_dates),
                        number="GF155",
                        status=FlightStatusEnum.EXPECTED,
                        departure_time=f"{departure_date} 12:00Z",
                    )
                ]
            )

        resolved = ResolvedFunctionCall(
            function_name="extract_flight_info",
            args={
                "airline_iata": "GF",
                "flight_number": "155",
                "departure_date": "2026-08-20",
            },
            handler=handler,
        )

        response = await _execute_search_with_date_fallback(
            resolved_call=resolved,
            query="GF155",
            session=MagicMock(),
            now=datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(requested_dates, ["2026-08-20", "2026-08-21"])
        self.assertEqual(response.flights_result[0].number, "GF155")
        self.assertEqual(resolved.args["departure_date"], "2026-08-21")

    async def test_airline_airport_search_checks_three_upcoming_dates(self):
        requested_dates = []

        async def handler(*, departure_date: str, session, **kwargs):
            requested_dates.append(departure_date)
            if departure_date != "2026-09-05":
                return QuerySearchResponse()
            return QuerySearchResponse(
                flights_result=[
                    self._flight(
                        identifier=9,
                        number="F81234",
                        status=FlightStatusEnum.EXPECTED,
                        departure_time="2026-09-05 12:00Z",
                    )
                ]
            )

        resolved = ResolvedFunctionCall(
            function_name="extract_flight_info_via_airport_single_derection",
            args={
                "airport_iata": "CUN",
                "direction": "Arrival",
                "departure_date": "2026-09-02",
                "airline_iata": "F8",
            },
            handler=handler,
        )

        response = await _execute_search_with_date_fallback(
            resolved_call=resolved,
            query="Flair Canada A Cancun Mexico",
            session=MagicMock(),
            now=datetime(2026, 9, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(
            requested_dates,
            ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"],
        )
        self.assertEqual(response.flights_result[0].number, "F81234")


if __name__ == "__main__":
    unittest.main()
