import unittest
from unittest import mock

from fastapi import HTTPException

from core.services.flight.api_client import (
    AerodataboxClient,
    AerodataboxUnavailableError,
)
from core.services.flight.service import FlightService


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


class AerodataboxClientAsyncTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _client_with_responses(*responses):
        client = object.__new__(AerodataboxClient)
        client.client = mock.Mock()
        client.client.get = mock.AsyncMock(side_effect=responses)
        return client

    @staticmethod
    def _response(status_code, payload=None):
        response = mock.Mock(status_code=status_code)
        response.json.return_value = payload
        return response

    async def test_both_failed_windows_raise_provider_unavailable(self):
        client = self._client_with_responses(
            self._response(502),
            self._response(502),
        )

        with self.assertRaises(AerodataboxUnavailableError) as raised:
            await client.get_airport_flights("EVN", "2026-08-02", "Departure")

        self.assertEqual(
            raised.exception.failures,
            ("morning:status_502", "afternoon:status_502"),
        )

    async def test_partial_nonempty_result_is_returned_when_other_window_fails(self):
        client = self._client_with_responses(
            self._response(
                200,
                {
                    "departures": [
                        {
                            "number": "3F323",
                            "status": "Departed",
                        }
                    ],
                    "arrivals": [],
                },
            ),
            self._response(502),
        )

        result = await client.get_airport_flights(
            "EVN", "2026-08-02", "Departure"
        )

        self.assertEqual([flight.number for flight in result.departures], ["3F323"])

    async def test_empty_successful_windows_remain_a_valid_empty_result(self):
        client = self._client_with_responses(
            self._response(200, {"departures": [], "arrivals": []}),
            self._response(200, {"departures": [], "arrivals": []}),
        )

        result = await client.get_airport_flights(
            "EVN", "2026-08-02", "Departure"
        )

        self.assertEqual(result.departures, [])
        self.assertEqual(result.arrivals, [])

    async def test_empty_success_and_failed_window_raise_provider_unavailable(self):
        client = self._client_with_responses(
            self._response(200, {"departures": [], "arrivals": []}),
            self._response(502),
        )

        with self.assertRaises(AerodataboxUnavailableError):
            await client.get_airport_flights("EVN", "2026-08-02", "Departure")

    @mock.patch("core.services.flight.service.AerodataboxClient")
    async def test_flight_service_maps_provider_failure_to_503(self, client_class):
        client_class.return_value.get_airport_flights = mock.AsyncMock(
            side_effect=AerodataboxUnavailableError(
                ["morning:status_502", "afternoon:status_502"]
            )
        )

        with self.assertRaises(HTTPException) as raised:
            await FlightService.get_airport_flights(
                airport_iata="EVN",
                departure_date="2026-08-02",
                direction="Departure",
            )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            "Flight data provider temporarily unavailable",
        )


if __name__ == "__main__":
    unittest.main()
