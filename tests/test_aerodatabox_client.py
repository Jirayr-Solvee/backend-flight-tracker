import unittest

from core.services.flight.api_client import AerodataboxClient


class AerodataboxClientTests(unittest.TestCase):
    def test_airport_payload_skips_malformed_flight_without_dropping_valid_results(
        self,
    ):
        payload = {
            "departures": [
                {
                    "number": "AA123",
                    "status": "Expected",
                },
                {
                    "departure": {"scheduledTime": {"local": "2026-07-28 12:00"}},
                    "airline": {"name": "Unknown/Private owner"},
                    "status": "Expected",
                },
                {
                    "number": "DL456",
                    "status": "EnRoute",
                },
            ],
            "arrivals": [],
        }

        result = AerodataboxClient._parse_airport_fids_payload(
            payload,
            airport_iata="LAX",
            departure_date="2026-07-28",
            time_window="afternoon",
        )

        self.assertEqual(
            [flight.number for flight in result.departures or []],
            ["AA123", "DL456"],
        )

    def test_airport_payload_ignores_invalid_collection_shape(self):
        result = AerodataboxClient._parse_airport_fids_payload(
            {"departures": {"number": "AA123"}, "arrivals": None},
            airport_iata="LAX",
            departure_date="2026-07-28",
            time_window="morning",
        )

        self.assertEqual(result.departures, [])
        self.assertEqual(result.arrivals, [])


if __name__ == "__main__":
    unittest.main()
