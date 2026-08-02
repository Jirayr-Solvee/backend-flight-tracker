import importlib.util
import json
import pathlib
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).with_name("lambda_function.py")
SPEC = importlib.util.spec_from_file_location("sofly_api_monitor", MODULE_PATH)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        monitor.load_secret.cache_clear()
        monitor.secrets_client.cache_clear()

    @mock.patch.object(monitor.time, "sleep")
    @mock.patch.object(monitor, "dns_preflight", return_value=["56.228.47.127"])
    @mock.patch.object(monitor, "load_guest_key", return_value="guest-key")
    def test_successful_search(self, _guest_key, _dns, _sleep):
        responses = [
            (200, json.dumps({"jwt": "temporary-jwt"})),
            (
                200,
                json.dumps(
                    {
                        "flights_result": [],
                        "airport_flights_result": [
                            {
                                "number": "3F 323",
                                "status": "Departed",
                                "departure": {"airport": {"iata": "EVN"}},
                                "arrival": {"airport": {"iata": "VKO"}},
                            }
                        ],
                    }
                ),
            ),
            (200, ""),
        ]
        with mock.patch.object(monitor, "http_request", side_effect=responses):
            result = monitor.run_monitor()

        self.assertTrue(result["healthy"])
        self.assertEqual(result["airport_flights_result_count"], 1)
        self.assertEqual(result["samples"][0]["route"], "EVN->VKO")

    @mock.patch.object(monitor.time, "sleep")
    @mock.patch.object(monitor, "dns_preflight", return_value=["56.228.47.127"])
    @mock.patch.object(monitor, "load_guest_key", return_value="guest-key")
    def test_transient_timeout_retries_three_times(self, _guest_key, _dns, sleep):
        failure = monitor.MonitorFailure("http", "timed out", transient=True)
        with mock.patch.object(monitor, "http_request", side_effect=failure):
            result = monitor.run_monitor()

        self.assertFalse(result["healthy"])
        self.assertEqual(result["failed_stage"], "http")
        self.assertEqual(result["retry_count"], 2)
        self.assertEqual(len(result["attempts"]), 3)
        self.assertEqual(sleep.call_count, 2)

    @mock.patch.object(monitor.time, "sleep")
    @mock.patch.object(monitor, "dns_preflight", return_value=["56.228.47.127"])
    @mock.patch.object(monitor, "load_guest_key", return_value="guest-key")
    def test_empty_results_retry_then_recover(self, _guest_key, _dns, sleep):
        responses = [
            (200, json.dumps({"jwt": "temporary-jwt-1"})),
            (200, json.dumps({"flights_result": [], "airport_flights_result": []})),
            (200, ""),
            (200, json.dumps({"jwt": "temporary-jwt-2"})),
            (
                200,
                json.dumps(
                    {
                        "flights_result": [],
                        "airport_flights_result": [{"number": "3F323"}],
                    }
                ),
            ),
            (200, ""),
        ]

        with mock.patch.object(monitor, "http_request", side_effect=responses):
            result = monitor.run_monitor()

        self.assertTrue(result["healthy"])
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(len(result["attempts"]), 2)
        sleep.assert_called_once_with(1)

    @mock.patch.object(monitor.time, "sleep")
    @mock.patch.object(monitor, "dns_preflight", return_value=["56.228.47.127"])
    @mock.patch.object(monitor, "load_guest_key", return_value="guest-key")
    def test_persistent_empty_results_fail_after_retries(
        self, _guest_key, _dns, sleep
    ):
        responses = []
        for attempt in range(3):
            responses.extend(
                [
                    (200, json.dumps({"jwt": f"temporary-jwt-{attempt}"})),
                    (
                        200,
                        json.dumps(
                            {"flights_result": [], "airport_flights_result": []}
                        ),
                    ),
                    (200, ""),
                ]
            )

        with mock.patch.object(monitor, "http_request", side_effect=responses):
            result = monitor.run_monitor()

        self.assertFalse(result["healthy"])
        self.assertEqual(result["failed_stage"], "empty_results")
        self.assertEqual(result["retry_count"], 2)
        self.assertEqual(len(result["attempts"]), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_failure_message_contains_diagnostics(self):
        text = monitor.format_failure(
            {
                "failed_stage": "http",
                "error": "timed out",
                "retry_count": 2,
                "attempts": [
                    {
                        "dns_addresses": ["56.228.47.127"],
                        "guest_status": None,
                        "search_status": None,
                    }
                ],
            }
        )

        self.assertIn("Sofly API monitor failed", text)
        self.assertIn("Stage: http", text)
        self.assertIn("Retries: 2", text)


if __name__ == "__main__":
    unittest.main()
