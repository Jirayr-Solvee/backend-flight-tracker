import os
import unittest
from pathlib import Path


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

    def test_empty_result_does_not_guess_an_unconfirmed_airline_code(self):
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

        self.assertEqual(recovery.reason, "flight_not_found")
        self.assertEqual(recovery.suggestions, [])

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


if __name__ == "__main__":
    unittest.main()
