"""Synthetic synchronous SES budgets and transition contracts; no live providers.

The fake clock replaces only the Lambda module's clock, never asyncio's clock.
Local backend integration reuses disposable fixtures and a stubbed Gemini SDK.
These tests do not establish SES retry, SMTP, IAM, or 30-second response behavior.
"""

import copy
from contextlib import ExitStack
import io
import json
import logging
import os
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from tests import test_email_ingress as legacy


LAMBDA = legacy.LAMBDA
contract = legacy.contract
STOP = {"disposition": "STOP_RULE_SET"}
FAILURE = "Email intake delivery failed"
CHUNK = 64 * 1024
MODES = ("Event", "RequestResponse")


class Clock:
    def __init__(self):
        self.value = 100.0

    def monotonic(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ObservedBody(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []
        self.socket_timeouts = []
        self.close_calls = 0
        self.on_read = None
        self.on_close = None
        self.on_timeout = None

    def read(self, amount=-1):
        self.read_sizes.append(amount)
        value = super().read(amount)
        if self.on_read is not None:
            self.on_read(self, amount, value)
        return value

    def set_socket_timeout(self, value):
        self.socket_timeouts.append(value)
        if self.on_timeout is not None:
            self.on_timeout(value)

    def close(self):
        self.close_calls += 1
        super().close()
        if self.on_close is not None:
            self.on_close()


class Harness:
    def __init__(self, mode="RequestResponse", *, data=None, event=None):
        self.clock = Clock()
        self.runtime_ms = 25_000
        self.remaining = MagicMock(side_effect=lambda: self.runtime_ms)
        self.context = SimpleNamespace(invoked_function_arn=legacy.FUNCTION_ARN,
                                       get_remaining_time_in_millis=self.remaining)
        self.event = legacy.ses_event(invocation_type=mode) if event is None else event
        self.data = legacy.raw_email() if data is None else data
        self.body = ObservedBody(self.data)
        self.obj = {"Body": self.body, "ContentLength": len(self.data), "ETag": legacy.etag(self.data)}
        self.s3 = MagicMock()
        self.s3.get_object.return_value = self.obj
        self.client = MagicMock(return_value=self.s3)
        self.response = legacy.response()
        self.opener = MagicMock()
        self.opener.open.return_value = self.response
        self.log = io.StringIO()
        self.log_handler = logging.StreamHandler(self.log)

    def __enter__(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, legacy.ENV, clear=True))
        self.stack.enter_context(patch.object(LAMBDA, "time", SimpleNamespace(monotonic=self.clock.monotonic)))
        self.stack.enter_context(patch.object(LAMBDA.boto3, "client", self.client))
        self.stack.enter_context(patch.object(LAMBDA.urllib.request, "build_opener", return_value=self.opener))
        self.stack.enter_context(patch.object(LAMBDA.logger, "propagate", False))
        LAMBDA.logger.addHandler(self.log_handler)
        return self

    def __exit__(self, *args):
        LAMBDA.logger.removeHandler(self.log_handler)
        self.log_handler.close()
        self.stack.__exit__(*args)
        # Unused fixture streams are not production cleanup evidence.
        if not self.body.closed:
            io.BytesIO.close(self.body)

    def invoke(self):
        return LAMBDA.lambda_handler(self.event, self.context)


class SyncActionContractTests(unittest.TestCase):
    def extract(self, event):
        return contract.extract_ses_receipt(event, legacy.CONFIG,
                                           function_arn=legacy.FUNCTION_ARN,
                                           invoked_function_arn=legacy.FUNCTION_ARN)

    def test_event_to_sync_mode_change_preserves_canonical_receipt_identity(self):
        event = legacy.ses_event(invocation_type="Event")
        synchronous = copy.deepcopy(event)
        synchronous["Records"][0]["ses"]["receipt"]["action"]["invocationType"] = "RequestResponse"
        self.assertEqual(self.extract(event), self.extract(synchronous))
        for item in (event, synchronous):
            with Harness(event=item) as harness:
                self.assertEqual(harness.invoke(), STOP)
                payload = json.loads(harness.opener.open.call_args.args[0].data)
                contract.validate_notification(payload, legacy.CONFIG)

    def test_exact_action_shape_and_modes_fail_closed_before_storage_in_both_modes(self):
        mutations = (
            lambda action: None,
            lambda action: [],
            lambda action: "Lambda",
            lambda action: {},
            lambda action: {key: value for key, value in action.items() if key != "type"},
            lambda action: {key: value for key, value in action.items() if key != "functionArn"},
            lambda action: {key: value for key, value in action.items() if key != "invocationType"},
            lambda action: dict(action, type="lambda"),
            lambda action: dict(action, type="S3"),
            lambda action: dict(action, functionArn=legacy.FUNCTION_ARN + "-other"),
            lambda action: dict(action, unexpected="not-authority"),
            lambda action: dict(action, invocationType="DryRun"),
            lambda action: dict(action, invocationType="event"),
            lambda action: dict(action, invocationType="RequestResponse "),
            lambda action: dict(action, invocationType=None),
            lambda action: dict(action, invocationType=True),
            lambda action: dict(action, invocationType=0),
            lambda action: dict(action, invocationType=[]),
        )
        for mode in MODES:
            for index, mutate in enumerate(mutations):
                event = legacy.ses_event(invocation_type=mode)
                receipt = event["Records"][0]["ses"]["receipt"]
                receipt["action"] = mutate(receipt["action"])
                with self.subTest(mode=mode, mutation=index):
                    with self.assertRaises(contract.EmailIngressRejected):
                        self.extract(event)
                    with Harness(event=event) as harness:
                        self.assertEqual(harness.invoke(), STOP)
                        harness.client.assert_not_called()
                        harness.opener.open.assert_not_called()

    def test_both_modes_preserve_source_recipient_dmarc_and_header_guards(self):
        mutations = (
            lambda r: r.update(eventSource="aws:s3"),
            lambda r: r.update(eventVersion="2.0"),
            lambda r: r.update(s3={"not": "direct SES"}),
            lambda r: r["ses"]["receipt"].update(recipients=[legacy.RECIPIENT, "other@example.invalid"]),
            lambda r: r["ses"]["mail"].update(destination=["other@example.invalid"]),
            lambda r: r["ses"]["receipt"].update(dmarcVerdict={"status": "FAIL"}),
            lambda r: r["ses"]["receipt"].update(spamVerdict={"status": "GRAY"}),
            lambda r: r["ses"]["receipt"].update(virusVerdict={"status": "PROCESSING_FAILED"}),
            lambda r: r["ses"]["mail"].update(headersTruncated=0),
            lambda r: r["ses"]["mail"]["headers"].append({"name": "From", "value": legacy.SENDER}),
        )
        for mode in MODES:
            for index, mutate in enumerate(mutations):
                event = legacy.ses_event(invocation_type=mode)
                mutate(event["Records"][0])
                with self.subTest(mode=mode, mutation=index), Harness(event=event) as harness:
                    self.assertEqual(harness.invoke(), STOP)
                    harness.client.assert_not_called()
                    harness.opener.open.assert_not_called()


class SyncLambdaBudgetTests(unittest.TestCase):
    def assert_operational_failure(self, harness):
        with self.assertRaisesRegex(RuntimeError, "^" + FAILURE + "$") as caught:
            harness.invoke()
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.__cause__)
        for private in (legacy.PRIVATE, legacy.SECRET):
            self.assertNotIn(private, str(caught.exception))
            self.assertNotIn(private, harness.log.getvalue())
        self.assertNotIn("lambda_email_intake_accepted", harness.log.getvalue())
        return caught.exception

    def test_sync_uses_chunks_positive_timeouts_one_sdk_attempt_and_bound_payload(self):
        data = legacy.raw_email(body="x" * (2 * CHUNK + 100))
        with Harness(data=data) as harness:
            self.assertEqual(harness.invoke(), STOP)
            self.assertGreater(len(harness.body.read_sizes), 2)
            self.assertTrue(all(0 < amount <= CHUNK for amount in harness.body.read_sizes))
            self.assertEqual(harness.body.socket_timeouts, [3] * len(harness.body.read_sizes))
            self.assertEqual(harness.body.close_calls, 1)
            self.assertTrue(harness.body.closed)
            config = harness.client.call_args.kwargs["config"]
            self.assertEqual((config.connect_timeout, config.read_timeout), (3, 3))
            self.assertEqual(config.retries, {"total_max_attempts": 1})
            self.assertEqual(harness.opener.open.call_args.kwargs["timeout"], 10)
            request = harness.opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.sofly.to/emails/")
            self.assertEqual(request.get_method(), "POST")
            contract.validate_notification(json.loads(request.data), legacy.CONFIG)
            for private in (legacy.PRIVATE, legacy.SECRET):
                self.assertNotIn(private, request.data.decode())
                self.assertNotIn(private, harness.log.getvalue())

    def test_event_bypasses_runtime_callback_and_preserves_single_read_and_timeouts(self):
        with Harness(mode="Event") as harness:
            harness.remaining.side_effect = AssertionError("Event must not inspect remaining runtime")
            harness.body.on_read = lambda *_: harness.clock.advance(1000)
            self.assertEqual(harness.invoke(), STOP)
            harness.remaining.assert_not_called()
            self.assertEqual(harness.body.read_sizes, [contract.MAX_EMAIL_BYTES + 1])
            self.assertEqual(harness.body.socket_timeouts, [])
            config = harness.client.call_args.kwargs["config"]
            self.assertEqual((config.connect_timeout, config.read_timeout), (3, 10))
            self.assertEqual(config.retries, {"total_max_attempts": 1})
            self.assertEqual(harness.opener.open.call_args.kwargs["timeout"], 10)

    def test_sync_context_requires_callable_strict_integer_and_positive_reserve(self):
        class IntegerSubclass(int):
            pass
        for value in (None, True, False, "25000", 25_000.0, IntegerSubclass(25_000), -1, 0, 1999, 2000):
            with self.subTest(value=repr(value)), Harness() as harness:
                harness.runtime_ms = value
                self.assert_operational_failure(harness)
                harness.client.assert_not_called()
                harness.opener.open.assert_not_called()
        for value in (None, 25_000, "not-callable"):
            with self.subTest(callback=repr(value)), Harness() as harness:
                harness.context.get_remaining_time_in_millis = value
                self.assert_operational_failure(harness)
                harness.client.assert_not_called()
        with Harness() as harness:
            del harness.context.get_remaining_time_in_millis
            self.assert_operational_failure(harness)
            harness.client.assert_not_called()

    def test_context_callback_exception_is_sanitized_before_any_storage(self):
        with Harness() as harness:
            harness.remaining.side_effect = RuntimeError(legacy.PRIVATE + legacy.SECRET)
            self.assert_operational_failure(harness)
            harness.client.assert_not_called()

    def test_initial_small_runtime_shrinks_every_transport_timeout(self):
        for runtime_ms, expected in ((2500, 0.5), (2001, 0.001)):
            with self.subTest(runtime_ms=runtime_ms), Harness() as harness:
                harness.runtime_ms = runtime_ms
                self.assertEqual(harness.invoke(), STOP)
                config = harness.client.call_args.kwargs["config"]
                self.assertAlmostEqual(config.connect_timeout, expected, places=9)
                self.assertAlmostEqual(config.read_timeout, expected, places=9)
                self.assertEqual(len(harness.body.socket_timeouts), len(harness.body.read_sizes))
                for timeout in harness.body.socket_timeouts:
                    self.assertAlmostEqual(timeout, expected, places=9)
                harness.opener.open.assert_called_once()
                self.assertAlmostEqual(harness.opener.open.call_args.kwargs["timeout"], expected, places=9)
                self.assertEqual(harness.log.getvalue(), "lambda_email_intake_accepted\n")

    def test_warm_invocations_get_fresh_deadline_after_success_or_failure_then_event_bypasses_it(self):
        for first_fails in (False, True):
            with self.subTest(first_fails=first_fails), Harness() as harness:
                if first_fails:
                    harness.body.on_read = lambda *_: harness.clock.advance(20)
                    self.assert_operational_failure(harness)
                    harness.opener.open.assert_not_called()
                else:
                    self.assertEqual(harness.invoke(), STOP)
                    harness.opener.open.assert_called_once()
                self.assertTrue(harness.body.closed)

                # Reuse the same loaded handler module and context, but a new
                # network response/body and a clock beyond the prior deadline.
                harness.clock.advance(100)
                harness.body = ObservedBody(harness.data)
                harness.obj["Body"] = harness.body
                harness.opener.open.reset_mock()
                harness.remaining.reset_mock()
                self.assertEqual(harness.invoke(), STOP)
                harness.opener.open.assert_called_once()
                self.assertEqual(harness.opener.open.call_args.kwargs["timeout"], 10)
                self.assertGreater(harness.remaining.call_count, 0)
                self.assertTrue(harness.body.closed)
                self.assertEqual(harness.body.close_calls, 1)

                harness.clock.advance(100)
                harness.body = ObservedBody(harness.data)
                harness.body.on_read = lambda *_: harness.clock.advance(1000)
                harness.obj["Body"] = harness.body
                harness.event["Records"][0]["ses"]["receipt"]["action"]["invocationType"] = "Event"
                harness.remaining.reset_mock()
                harness.remaining.side_effect = AssertionError("Warm Event must bypass sync budget")
                harness.opener.open.reset_mock()
                self.assertEqual(harness.invoke(), STOP)
                harness.opener.open.assert_called_once()
                harness.remaining.assert_not_called()
                self.assertEqual(harness.body.read_sizes, [contract.MAX_EMAIL_BYTES + 1])
                self.assertEqual(harness.body.socket_timeouts, [])
                self.assertTrue(harness.body.closed)

    def test_work_cap_includes_validation_time_and_equality_is_expired(self):
        original = LAMBDA.extract_ses_receipt
        for elapsed in (20, 20.001):
            with self.subTest(elapsed=elapsed), Harness() as harness:
                def extract(*args, **kwargs):
                    proof = original(*args, **kwargs)
                    harness.clock.advance(elapsed)
                    return proof
                with patch.object(LAMBDA, "extract_ses_receipt", side_effect=extract):
                    self.assert_operational_failure(harness)
                harness.client.assert_not_called()
                harness.opener.open.assert_not_called()

    def test_shrinking_runtime_is_rechecked_not_only_captured_at_entry(self):
        with Harness() as harness:
            harness.s3.get_object.side_effect = lambda **_: (setattr(harness, "runtime_ms", 3500) or harness.obj)
            self.assertEqual(harness.invoke(), STOP)
            self.assertEqual(harness.body.socket_timeouts, [1.5] * len(harness.body.read_sizes))
            self.assertEqual(harness.opener.open.call_args.kwargs["timeout"], 1.5)

    def test_expiry_during_client_construction_prevents_get_and_post(self):
        with Harness() as harness:
            harness.client.side_effect = lambda *_, **__: (harness.clock.advance(20) or harness.s3)
            self.assert_operational_failure(harness)
            harness.s3.get_object.assert_not_called()
            harness.opener.open.assert_not_called()

    def test_expiry_during_get_closes_body_without_reading_or_posting(self):
        for boundary in ("clock", "runtime"):
            with self.subTest(boundary=boundary), Harness() as harness:
                def get(**_):
                    if boundary == "clock":
                        harness.clock.advance(20)
                    else:
                        harness.runtime_ms = 2000
                    return harness.obj
                harness.s3.get_object.side_effect = get
                self.assert_operational_failure(harness)
                self.assertEqual(harness.body.read_sizes, [])
                self.assertEqual(harness.body.close_calls, 1)
                self.assertTrue(harness.body.closed)
                harness.opener.open.assert_not_called()

    def test_expiry_or_invalid_runtime_after_first_chunk_stops_further_reads(self):
        for value in (2000, True, "25000"):
            with self.subTest(runtime=repr(value)), Harness(data=legacy.raw_email(body="x" * (CHUNK + 50))) as harness:
                harness.body.on_read = lambda *_: setattr(harness, "runtime_ms", value)
                self.assert_operational_failure(harness)
                self.assertEqual(harness.body.read_sizes, [CHUNK])
                self.assertTrue(harness.body.closed)
                harness.opener.open.assert_not_called()
        with Harness() as harness:
            harness.body.on_read = lambda *_: harness.clock.advance(20)
            self.assert_operational_failure(harness)
            self.assertEqual(harness.body.read_sizes, [CHUNK])
            self.assertEqual(harness.body.close_calls, 1)
            harness.opener.open.assert_not_called()

    def test_invalid_chunk_types_and_over_requested_bytes_are_terminal_not_posted(self):
        for chunk in (None, "not-bytes", bytearray(b"x"), b"x" * (CHUNK + 1)):
            with self.subTest(chunk_type=type(chunk).__name__), Harness() as harness:
                harness.body.read = MagicMock(return_value=chunk)
                self.assertEqual(harness.invoke(), STOP)
                harness.body.read.assert_called_once_with(CHUNK)
                self.assertTrue(harness.body.closed)
                harness.opener.open.assert_not_called()

    def test_short_body_and_invalid_lengths_never_post_and_always_close(self):
        for size in (None, True, 0, -1, contract.MAX_EMAIL_BYTES + 1, "42"):
            with self.subTest(size=repr(size)), Harness() as harness:
                harness.obj["ContentLength"] = size
                self.assertEqual(harness.invoke(), STOP)
                self.assertEqual(harness.body.read_sizes, [])
                self.assertEqual(harness.body.close_calls, 1)
                harness.opener.open.assert_not_called()
        with Harness() as harness:
            harness.obj["ContentLength"] += 1
            self.assertEqual(harness.invoke(), STOP)
            self.assertTrue(harness.body.closed)
            harness.opener.open.assert_not_called()

    def test_maximum_plus_one_cap_is_enforced_without_reading_rest(self):
        maximum = CHUNK + 7
        with Harness(data=b"x" * (maximum + 100)) as harness, patch.object(LAMBDA, "MAX_EMAIL_BYTES", maximum):
            harness.obj["ContentLength"] = maximum
            self.assertEqual(harness.invoke(), STOP)
            self.assertEqual(harness.body.read_sizes, [CHUNK, 8])
            self.assertEqual(harness.body.close_calls, 1)
            harness.opener.open.assert_not_called()

    def test_optional_socket_timeout_missing_or_noncallable_is_supported(self):
        for replacement in (None, 123, "not-callable"):
            with self.subTest(replacement=repr(replacement)), Harness() as harness:
                harness.body.set_socket_timeout = replacement
                self.assertEqual(harness.invoke(), STOP)
                self.assertEqual(harness.body.close_calls, 1)
                harness.opener.open.assert_called_once()
                self.assertEqual(harness.log.getvalue(), "lambda_email_intake_accepted\n")
        with Harness() as harness:
            stream = io.BytesIO(harness.data)
            harness.obj["Body"] = stream
            self.assertEqual(harness.invoke(), STOP)
            self.assertTrue(stream.closed)
            harness.opener.open.assert_called_once()
            self.assertEqual(harness.log.getvalue(), "lambda_email_intake_accepted\n")

    def test_socket_timeout_setter_exhausting_budget_prevents_actual_read(self):
        with Harness() as harness:
            harness.body.on_timeout = lambda _: harness.clock.advance(20)
            self.assert_operational_failure(harness)
            self.assertEqual(harness.body.socket_timeouts, [3])
            self.assertEqual(harness.body.read_sizes, [])
            self.assertEqual(harness.body.close_calls, 1)
            self.assertTrue(harness.body.closed)
            harness.opener.open.assert_not_called()

    def test_socket_setter_read_and_close_failures_are_bounded_and_cleanup_attempted(self):
        for stage in ("socket", "read", "close"):
            with self.subTest(stage=stage), Harness() as harness:
                def fail(*_):
                    raise RuntimeError(legacy.PRIVATE + legacy.SECRET)
                if stage == "socket":
                    harness.body.on_timeout = fail
                elif stage == "read":
                    harness.body.read = MagicMock(side_effect=fail)
                else:
                    harness.body.on_close = fail
                self.assert_operational_failure(harness)
                self.assertEqual(harness.body.close_calls, 1)
                self.assertTrue(harness.body.closed)
                harness.opener.open.assert_not_called()

    def test_get_failure_is_bounded_without_http_delivery(self):
        with Harness() as harness:
            harness.s3.get_object.side_effect = RuntimeError(legacy.PRIVATE + legacy.SECRET)
            self.assert_operational_failure(harness)
            harness.opener.open.assert_not_called()

    def test_binding_expiry_prevents_post_even_when_validation_rejects(self):
        original = LAMBDA.bind_object
        for reject in (False, True):
            with self.subTest(reject=reject), Harness() as harness:
                def bind(*args, **kwargs):
                    harness.clock.advance(20)
                    if reject:
                        raise contract.EmailIngressRejected()
                    return original(*args, **kwargs)
                with patch.object(LAMBDA, "bind_object", side_effect=bind):
                    self.assert_operational_failure(harness)
                self.assertTrue(harness.body.closed)
                harness.opener.open.assert_not_called()

    def test_validation_time_shrinks_post_timeout_and_expiry_prevents_post(self):
        original = LAMBDA.validate_notification
        for elapsed in (19.5, 20):
            with self.subTest(elapsed=elapsed), Harness() as harness:
                def validate(*args, **kwargs):
                    result = original(*args, **kwargs)
                    harness.clock.advance(elapsed)
                    return result
                with patch.object(LAMBDA, "validate_notification", side_effect=validate):
                    if elapsed < 20:
                        self.assertEqual(harness.invoke(), STOP)
                        self.assertEqual(harness.opener.open.call_args.kwargs["timeout"], 0.5)
                    else:
                        self.assert_operational_failure(harness)
                        harness.opener.open.assert_not_called()

    def test_expiry_in_post_or_response_cleanup_is_not_reported_as_acceptance(self):
        for stage in ("open", "exit"):
            with self.subTest(stage=stage), Harness() as harness:
                if stage == "open":
                    harness.opener.open.side_effect = lambda *_, **__: (harness.clock.advance(20) or harness.response)
                else:
                    harness.response.__exit__.side_effect = lambda *_: (harness.clock.advance(20) or False)
                self.assert_operational_failure(harness)
                harness.opener.open.assert_called_once()
                harness.response.__exit__.assert_called_once()
                self.assertTrue(harness.body.closed)

    def test_response_exit_exception_is_sanitized_without_retry(self):
        with Harness() as harness:
            harness.response.__exit__.side_effect = RuntimeError(legacy.PRIVATE + legacy.SECRET)
            self.assert_operational_failure(harness)
            harness.opener.open.assert_called_once()
            harness.response.__exit__.assert_called_once()

    def test_http_errors_close_once_and_distinguish_permanent_from_operational(self):
        for code in (301, 302, 303, 307, 308, 400, 401, 403, 404, 408, 422, 429, 500, 503):
            with self.subTest(code=code), Harness() as harness:
                stream = io.BytesIO(legacy.PRIVATE.encode())
                error = urllib.error.HTTPError("https://api.sofly.to/emails/", code, legacy.SECRET, {}, stream)
                error.close = MagicMock(wraps=error.close)
                harness.opener.open.side_effect = error
                if 400 <= code < 500 and code not in (408, 429):
                    self.assertEqual(harness.invoke(), STOP)
                else:
                    self.assert_operational_failure(harness)
                error.close.assert_called_once()
                self.assertTrue(stream.closed)
                harness.opener.open.assert_called_once()
                self.assertNotIn(legacy.PRIVATE, harness.log.getvalue())
                self.assertNotIn(legacy.SECRET, harness.log.getvalue())

    def test_permanent_http_error_cleanup_expiry_or_failure_is_operational(self):
        for failure in (False, True):
            with self.subTest(close_failure=failure), Harness() as harness:
                stream = io.BytesIO(legacy.PRIVATE.encode())
                error = urllib.error.HTTPError("https://api.sofly.to/emails/", 403, legacy.SECRET, {}, stream)
                original_close = error.close
                def close():
                    original_close()
                    if failure:
                        raise RuntimeError(legacy.PRIVATE + legacy.SECRET)
                    harness.clock.advance(20)
                error.close = MagicMock(side_effect=close)
                harness.opener.open.side_effect = error
                self.assert_operational_failure(harness)
                error.close.assert_called_once()
                self.assertTrue(stream.closed)
                self.assertNotIn("lambda_email_intake_rejected", harness.log.getvalue())

    def test_normal_responses_other_than_200_and_202_never_log_acceptance(self):
        for status in (201, 204, 301, 400, 500):
            with self.subTest(status=status), Harness() as harness:
                harness.response.status = status
                self.assert_operational_failure(harness)
                harness.response.__exit__.assert_called_once()
        for status in (200, 202):
            with self.subTest(status=status), Harness() as harness:
                harness.response.status = status
                self.assertEqual(harness.invoke(), STOP)
                self.assertEqual(harness.log.getvalue(), "lambda_email_intake_accepted\n")

    def test_unknown_transport_outcome_is_sanitized_and_never_auto_retried(self):
        for error in (urllib.error.URLError(legacy.PRIVATE + legacy.SECRET),
                      TimeoutError(legacy.PRIVATE + legacy.SECRET)):
            with self.subTest(error_type=type(error).__name__), Harness() as harness:
                harness.opener.open.side_effect = error
                self.assert_operational_failure(harness)
                harness.opener.open.assert_called_once()
                self.assertTrue(harness.body.closed)


class SyncLostAckTests(unittest.TestCase):
    # Reuse only setup helpers, not the legacy TestCase's test methods.
    setUp = legacy.EmailIngressTests.setUp
    tearDown = legacy.EmailIngressTests.tearDown
    grant = legacy.EmailIngressTests.grant

    def test_sync_lost_ack_then_cross_mode_replays_do_not_start_second_sdk_job(self):
        data = legacy.raw_email(body=legacy.PRIVATE)
        self.sdk.models.generate_content.return_value = SimpleNamespace(candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(function_call=SimpleNamespace(
                name="extract_flight_from_email",
                args={"flight_number": "100", "airline_iata": "AA", "departure_date": "2026-09-12"},
            ))]),
        )])
        async def handler(*, session, **kwargs):
            return [session.get(legacy.Flight, 42)]
        payloads = []
        opener = MagicMock()
        def deliver(request, **kwargs):
            payloads.append(json.loads(request.data))
            result = self.client.post("/emails/", headers={"Authorization": request.get_header("Authorization")},
                                      content=request.data)
            self.assertEqual(result.status_code, 202)
            if len(payloads) == 1:
                raise urllib.error.URLError(legacy.PRIVATE + legacy.SECRET)
            return legacy.response(result.status_code)
        opener.open.side_effect = deliver
        s3 = MagicMock()
        s3.get_object.side_effect = lambda **_: legacy.s3_object(data)
        event = legacy.ses_event(invocation_type="RequestResponse")
        context = SimpleNamespace(invoked_function_arn=legacy.FUNCTION_ARN,
                                  get_remaining_time_in_millis=lambda: 25_000)
        with patch.dict(os.environ, legacy.ENV, clear=True), \
                patch.object(legacy.settings, "LAMBDA_FUNCTION_AUTH_TOKEN", legacy.SECRET), \
                patch.object(LAMBDA, "time", SimpleNamespace(monotonic=lambda: 100.0)), \
                patch.object(LAMBDA.boto3, "client", return_value=s3), \
                patch.object(LAMBDA.urllib.request, "build_opener", return_value=opener), \
                patch.object(legacy.background_tasks, "get_s3_client", return_value=s3), \
                patch.dict("core.services.gemini.service.REQUIRED_FIELDS", {
                    "extract_flight_from_email": legacy.FunctionDefinition(
                        handler=handler, required_fields=["flight_number", "airline_iata", "departure_date"],
                    ),
                }):
            with self.assertRaisesRegex(RuntimeError, "^" + FAILURE + "$") as caught:
                LAMBDA.lambda_handler(event, context)
            self.assertTrue(caught.exception.__suppress_context__)
            opener.open.assert_called_once()
            self.assertEqual(self.sdk.models.generate_content.call_count, 1)
            for mode in MODES:
                replay = copy.deepcopy(event)
                replay["Records"][0]["ses"]["receipt"]["action"]["invocationType"] = mode
                self.assertEqual(LAMBDA.lambda_handler(replay, context), STOP)
                self.assertEqual(self.sdk.models.generate_content.call_count, 1)
        self.assertEqual(len(payloads), 3)
        self.assertEqual(payloads, [payloads[0]] * 3)
        with legacy.Session(self.engine) as session:
            links = session.exec(legacy.select(legacy.UserFlightLink).where(legacy.UserFlightLink.user_id == legacy.OWNER)).all()
            receipts = session.exec(legacy.select(legacy.UserAIEmailReceipt)).all()
            self.assertEqual(len(links), 1)
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0].state, "completed")


if __name__ == "__main__":
    unittest.main()
