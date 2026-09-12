"""Synthetic-only tests for private data at Gemini's parsing/retry boundary."""

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Install inert configuration before core imports; run in a clean temporary cwd.
from tests import test_experiment_reporting as _test_environment

from core.services.gemini.config import REQUIRED_FIELDS
from core.services.gemini.service import GeminiService


LOGGER_NAME = "core.services.gemini.service"
API_KEY = "FAKE_GEMINI_CREDENTIAL_DO_NOT_LOG"
EMAIL = "synthetic-private@example.invalid"
QUERY = "PRIVATE_SYNTHETIC_ITINERARY_DO_NOT_LOG"
PROVIDER_BODY = "PRIVATE_SYNTHETIC_PROVIDER_RESPONSE_DO_NOT_LOG"
SENTINELS = (API_KEY, EMAIL, QUERY, PROVIDER_BODY)
PRIVATE_TEXT = " ".join(SENTINELS)


def response_with_function(name, args):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(
        parts=[SimpleNamespace(function_call=SimpleNamespace(name=name, args=args))],
    ))])


def unsafe_provider_error():
    error = RuntimeError(PRIVATE_TEXT)
    error.__cause__ = ValueError("chained provider request " + PRIVATE_TEXT)
    return error


class GeminiCredentialLoggingTests(unittest.TestCase):
    def setUp(self):
        # Never construct an SDK client; every provider call is mocked below.
        self.service = GeminiService.__new__(GeminiService)
        self.service.client = object()

    def assert_private_logs(self, capture):
        self.assertTrue(capture.records)
        for record in capture.records:
            rendered = logging.Formatter("%(levelname)s %(message)s").format(record)
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, rendered)
                self.assertNotIn(sentinel, repr(record.__dict__))
            self.assertFalse(record.exc_info)
            self.assertIsNone(record.stack_info)

    def get_function_call(self, email):
        return asyncio.run(self.service.get_function_call(PRIVATE_TEXT, email=email))

    def test_provider_exception_and_chained_private_text_are_bounded_in_both_modes(self):
        for email in (False, True):
            with self.subTest(email=email), \
                    patch.object(self.service, "_deterministic_function_call", return_value=None), \
                    patch.object(self.service, "_generate", new=AsyncMock(
                        side_effect=unsafe_provider_error(),
                    )) as generate, self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
                result = self.get_function_call(email)

            self.assertIsNone(result)
            self.assertEqual(generate.await_count, 3)
            self.assert_private_logs(logs)
            errors = [record for record in logs.records if record.levelno == logging.ERROR]
            self.assertEqual([record.getMessage() for record in errors], [
                f"Error retrieving Gemini function call email_mode={email} attempt={attempt}"
                for attempt in range(3)
            ])
            self.assertEqual(logs.records[-1].getMessage(),
                             f"Gemini unable to extract a function call email_mode={email} attempts=3")

    def test_unregistered_name_is_bounded_in_helper(self):
        with self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            accepted = self.service._validate_function_args(
                function_name=PRIVATE_TEXT, args={"private": PRIVATE_TEXT},
            )
        self.assertFalse(accepted)
        self.assert_private_logs(logs)
        self.assertEqual([record.getMessage() for record in logs.records],
                         ["Gemini produced an unregistered function"])

    def test_unregistered_provider_name_is_not_echoed_by_invalid_call_retry_log(self):
        response = response_with_function(PRIVATE_TEXT, {"private": PRIVATE_TEXT})
        for email in (False, True):
            with self.subTest(email=email), \
                    patch.object(self.service, "_deterministic_function_call", return_value=None), \
                    patch.object(self.service, "_generate", new=AsyncMock(
                        return_value=response,
                    )) as generate, self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
                result = self.get_function_call(email)

            self.assertIsNone(result)
            self.assertEqual(generate.await_count, 3)
            self.assert_private_logs(logs)
            self.assertEqual(len(logs.records), 7)
            invalid_calls = [record.getMessage() for record in logs.records
                             if "invalid function call" in record.getMessage()]
            self.assertEqual(invalid_calls, [
                f"Gemini produced an invalid function call email_mode={email} attempt={attempt}"
                for attempt in range(3)
            ])

    def test_known_function_missing_fields_do_not_log_argument_values(self):
        response = response_with_function("extract_flight_from_email", {
            "flight_number": PRIVATE_TEXT,
        })
        with patch.object(self.service, "_generate", new=AsyncMock(return_value=response)), \
                self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            result = self.get_function_call(email=True)
        self.assertIsNone(result)
        self.assert_private_logs(logs)
        self.assertIn("Missing fields ['airline_iata', 'departure_date'] "
                      "for function extract_flight_from_email", logs.records[0].getMessage())

    def test_private_parser_exception_is_also_bounded(self):
        class MalformedResponse:
            @property
            def candidates(self):
                raise unsafe_provider_error()

        with patch.object(self.service, "_generate", new=AsyncMock(
            return_value=MalformedResponse(),
        )) as generate, self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            result = self.get_function_call(email=True)
        self.assertIsNone(result)
        self.assertEqual(generate.await_count, 3)
        self.assert_private_logs(logs)

    def test_retry_can_still_resolve_without_logging_payload_or_invoking_handler(self):
        args = {"flight_number": "123", "airline_iata": "BA", "departure_date": "2026-09-12"}
        response = response_with_function("extract_flight_from_email", args)
        with patch.object(self.service, "_generate", new=AsyncMock(
            side_effect=[unsafe_provider_error(), response],
        )) as generate, self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            result = self.get_function_call(email=True)
        self.assertIsNotNone(result)
        self.assertEqual(result.function_name, "extract_flight_from_email")
        self.assertEqual(result.args, args)
        self.assertEqual(result.handler, REQUIRED_FIELDS["extract_flight_from_email"].handler)
        self.assertEqual(generate.await_count, 2)
        self.assertEqual(len(logs.records), 1)
        self.assert_private_logs(logs)


if __name__ == "__main__":
    unittest.main()
