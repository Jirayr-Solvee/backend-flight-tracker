"""Real S3 SDK streaming over loopback; synthetic data and credentials only.

The backend POST is stubbed. These tests prove the installed SDK's socket/EOF
lifecycle, not AWS IAM, SES delivery, SMTP semantics, or production processing.
SDK exception records stay in memory and never propagate to the test output.
"""

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import logging
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import boto3
from botocore.config import Config
from botocore.response import StreamingBody

ROOT = Path(__file__).resolve().parents[1]
# Load the pure shared contract without core.__init__ and its database setup.
CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "synthetic_sdk_stream_contract", ROOT / "core/email_ingress_contract.py",
)
contract = importlib.util.module_from_spec(CONTRACT_SPEC)
with patch.dict("sys.modules", {CONTRACT_SPEC.name: contract}):
    CONTRACT_SPEC.loader.exec_module(contract)

SOURCE = ROOT / "lambda/lambda_function.py"
SPEC = importlib.util.spec_from_file_location("synthetic_sdk_stream_lambda", SOURCE)
LAMBDA = importlib.util.module_from_spec(SPEC)
with patch.dict("sys.modules", {"email_ingress_contract": contract}):
    SPEC.loader.exec_module(LAMBDA)

BUCKET = "fixture-sdk-bucket"
SENDER = "synthetic-sdk-owner@example.invalid"
RECIPIENT = "track@sofly.to"
FUNCTION_ARN = "arn:aws:lambda:eu-north-1:123456789012:function:incoming_email_handler"
TOKEN = "SYNTHETIC_SDK_BACKEND_TOKEN"
MESSAGE_ID = "synthetic-sdk-stream-message"
VERSION = "synthetic-sdk-version"
CHUNK = 64 * 1024
STOP = {"disposition": "STOP_RULE_SET"}
FAILURE = "Email intake delivery failed"
CONFIG = contract.EmailIngressConfig(BUCKET, contract.DEFAULT_KEY_PREFIX, RECIPIENT)
ENV = {
    "BACKEND_URL": "https://api.sofly.to", "LAMBDA_FUNCTION_AUTH_TOKEN": TOKEN,
    "FORWARDED_EMAIL_BUCKET": BUCKET, "FORWARDED_EMAIL_KEY_PREFIX": contract.DEFAULT_KEY_PREFIX,
    "FORWARDED_EMAIL_RECIPIENT": RECIPIENT, "FORWARDED_EMAIL_LAMBDA_ARN": FUNCTION_ARN,
}


def raw_email(size=None):
    data = ("From: " + SENDER + "\r\nTo: " + RECIPIENT +
            "\r\n\r\nSYNTHETIC_PRIVATE_SDK_BODY\r\n").encode()
    return data if size is None else data + b"x" * (size - len(data))


def ses_event(mode="RequestResponse"):
    timestamp = datetime.fromtimestamp((contract.now_ms() - 100) / 1000, timezone.utc).isoformat()
    return {"Records": [{"eventSource": "aws:ses", "eventVersion": "1.0", "ses": {
        "mail": {"timestamp": timestamp, "messageId": MESSAGE_ID,
                 "destination": [RECIPIENT], "headersTruncated": False,
                 "headers": [{"name": "From", "value": SENDER}],
                 "commonHeaders": {"from": [SENDER]}},
        "receipt": {"timestamp": timestamp, "recipients": [RECIPIENT],
                    **{name: {"status": "PASS"} for name in
                       ("spamVerdict", "virusVerdict", "spfVerdict", "dkimVerdict", "dmarcVerdict")},
                    "action": {"type": "Lambda", "functionArn": FUNCTION_ARN,
                               "invocationType": mode}},
    }}]}


class MemoryLog(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class LoopbackS3:
    """HTTP framing is real; only explicit metadata corruption is injected."""

    def __init__(self, data, *, wire_size=None, metadata_size=None, setter_failure=False):
        self.data = data
        self.wire_size = len(data) if wire_size is None else wire_size
        self.metadata_size = metadata_size
        self.setter_failure = setter_failure
        self.etag = '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'
        self.requests = []
        self.server_errors = 0
        self.clients = []
        self.bodies = []
        self.read_results = []
        self.sdk_log = MemoryLog()
        self.lambda_log = MemoryLog()
        self.opener = MagicMock()
        response = MagicMock(status=202)
        response.__enter__.return_value = response
        self.opener.open.return_value = response
        self.session = boto3.session.Session(
            aws_access_key_id="SYNTHETIC_SDK_ACCESS_KEY",
            aws_secret_access_key="SYNTHETIC_SDK_SECRET_KEY",
            region_name="eu-north-1",
        )

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                fixture.requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", str(fixture.wire_size))
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("ETag", fixture.etag)
                self.send_header("x-amz-version-id", VERSION)
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(fixture.data)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # Expected when an invalid declared length is rejected early.
                    pass
                self.close_connection = True

        class Server(ThreadingHTTPServer):
            daemon_threads = False

            def handle_error(self, request, client_address):
                fixture.server_errors += 1

        self.stack = ExitStack()
        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.stack.enter_context(patch.dict(os.environ, ENV, clear=True))
        self.stack.enter_context(patch.object(LAMBDA.boto3, "client", side_effect=self.client))
        self.stack.enter_context(patch.object(LAMBDA.urllib.request, "build_opener", return_value=self.opener))
        for logger, handler in ((logging.getLogger("botocore.response"), self.sdk_log),
                                (LAMBDA.logger, self.lambda_log)):
            self.stack.enter_context(patch.object(logger, "propagate", False))
            logger.addHandler(handler)
            self.stack.callback(logger.removeHandler, handler)
            self.stack.callback(handler.close)
        return self

    def __exit__(self, *args):
        try:
            for body in self.bodies:
                if not body._raw_stream.closed:
                    body.close()
            for client in self.clients:
                client.close()
        finally:
            self.stack.__exit__(*args)
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)

    def client(self, service, *, region_name, config):
        assert service == "s3" and region_name == "eu-north-1"
        self.handler_config = config
        self.handler_retries = dict(config.retries)
        # Preserve production timeout/retry settings, but force loopback routing
        # and bypass machine proxy configuration with synthetic signing keys.
        client = self.session.client(
            service, region_name=region_name,
            endpoint_url="http://127.0.0.1:" + str(self.server.server_port),
            config=config.merge(Config(proxies={}, s3={"addressing_style": "path"})),
        )
        self.clients.append(client)
        get_object = client.get_object

        def observed_get_object(**kwargs):
            obj = get_object(**kwargs)
            body = obj["Body"]
            if not isinstance(body, StreamingBody):
                raise AssertionError("Loopback did not return a real SDK stream")
            self.bodies.append(body)
            read = body.read

            def observed_read(amount):
                chunk = read(amount)
                self.read_results.append((amount, len(chunk), body._raw_stream.closed))
                return chunk

            self.stack.enter_context(patch.object(body, "read", side_effect=observed_read))
            setter = body.set_socket_timeout
            self.stack.enter_context(patch.object(
                body, "set_socket_timeout", wraps=setter,
                **({"side_effect": AttributeError("SYNTHETIC_PRIVATE_SETTER_FAILURE")}
                   if self.setter_failure else {}),
            ))
            self.stack.enter_context(patch.object(body, "close", wraps=body.close))
            if self.metadata_size is not None:
                obj["ContentLength"] = self.metadata_size
            return obj

        self.stack.enter_context(patch.object(client, "get_object", side_effect=observed_get_object))
        return client

    def invoke(self, mode="RequestResponse"):
        self.remaining = MagicMock(return_value=25_000)
        self.event = ses_event(mode)
        context = SimpleNamespace(invoked_function_arn=FUNCTION_ARN,
                                  get_remaining_time_in_millis=self.remaining)
        return LAMBDA.lambda_handler(self.event, context)


class RealSDKStreamTests(unittest.TestCase):
    def assert_closed_once(self, harness):
        self.assertEqual(len(harness.bodies), 1)
        harness.bodies[0].close.assert_called_once_with()
        self.assertTrue(harness.bodies[0]._raw_stream.closed)
        self.assertEqual(harness.server_errors, 0)
        self.assertEqual(harness.requests, ["/" + BUCKET + "/" + contract.DEFAULT_KEY_PREFIX + MESSAGE_ID])
        self.assertEqual(harness.handler_retries, {"total_max_attempts": 1})

    def assert_accepted(self, harness):
        self.assert_closed_once(harness)
        self.assertEqual(len(harness.sdk_log.records), 0)
        self.assertEqual([record.getMessage() for record in harness.lambda_log.records],
                         ["lambda_email_intake_accepted"])
        harness.opener.open.assert_called_once()
        request = harness.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.sofly.to/emails/")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        payload = json.loads(request.data)
        contract.validate_notification(payload, CONFIG)
        expected = contract.bind_object(contract.extract_ses_receipt(
            harness.event, CONFIG, function_arn=FUNCTION_ARN, invoked_function_arn=FUNCTION_ARN,
        ), harness.data, harness.etag, VERSION)
        self.assertEqual(payload, {"bucket": BUCKET, "key": contract.DEFAULT_KEY_PREFIX + MESSAGE_ID,
                                   "receipt": expected})
        self.assertNotIn(b"SYNTHETIC_PRIVATE_SDK_BODY", request.data)
        self.assertNotIn(TOKEN.encode(), request.data)

    def test_small_complete_object_keeps_eof_read_without_retuning_released_socket(self):
        with LoopbackS3(raw_email()) as harness:
            self.assertEqual(harness.invoke(), STOP)
            self.assert_accepted(harness)
            self.assertEqual(harness.read_results, [(CHUNK, len(harness.data), True), (CHUNK, 0, True)])
            harness.bodies[0].set_socket_timeout.assert_called_once()

    def test_multiple_chunks_retune_only_before_declared_boundary_and_preserve_eof(self):
        with LoopbackS3(raw_email(2 * CHUNK + 37)) as harness:
            self.assertEqual(harness.invoke(), STOP)
            self.assert_accepted(harness)
            self.assertEqual(harness.read_results, [(CHUNK, CHUNK, False), (CHUNK, CHUNK, False),
                                                    (CHUNK, 37, True), (CHUNK, 0, True)])
            self.assertEqual(harness.bodies[0].set_socket_timeout.call_count, 3)
            self.assertTrue(all(0 < call.args[0] <= 3
                                for call in harness.bodies[0].set_socket_timeout.call_args_list))

    def test_exact_chunk_boundary_still_checks_eof_without_sdk_exception(self):
        with LoopbackS3(raw_email(CHUNK)) as harness:
            self.assertEqual(harness.invoke(), STOP)
            self.assert_accepted(harness)
            self.assertEqual(harness.read_results, [(CHUNK, CHUNK, True), (CHUNK, 0, True)])
            harness.bodies[0].set_socket_timeout.assert_called_once()

    def test_event_mode_preserves_one_bounded_read_and_never_inspects_runtime(self):
        with LoopbackS3(raw_email()) as harness:
            self.assertEqual(harness.invoke("Event"), STOP)
            self.assert_accepted(harness)
            harness.remaining.assert_not_called()
            harness.bodies[0].set_socket_timeout.assert_not_called()
            self.assertEqual(harness.read_results, [(contract.MAX_EMAIL_BYTES + 1, len(harness.data), True)])
            self.assertEqual(harness.handler_config.read_timeout, 10)

    def test_empty_declared_object_is_closed_without_read_or_post(self):
        with LoopbackS3(b"") as harness:
            self.assertEqual(harness.invoke(), STOP)
            self.assert_closed_once(harness)
            harness.bodies[0].read.assert_not_called()
            harness.bodies[0].set_socket_timeout.assert_not_called()
            harness.opener.open.assert_not_called()
            self.assertEqual(len(harness.sdk_log.records), 0)

    def test_truncated_wire_response_remains_sanitized_operational_failure(self):
        data = raw_email()
        with LoopbackS3(data[:-10], wire_size=len(data)) as harness:
            with self.assertRaises(RuntimeError) as caught:
                harness.invoke()
            self.assertEqual(str(caught.exception), FAILURE)
            self.assertTrue(caught.exception.__suppress_context__)
            self.assert_closed_once(harness)
            harness.opener.open.assert_not_called()
            self.assertEqual(len(harness.lambda_log.records), 0)

    def test_lower_metadata_length_still_rejects_real_stream_overrun(self):
        data = raw_email()
        with LoopbackS3(data, metadata_size=len(data) - 1) as harness:
            self.assertEqual(harness.invoke(), STOP)
            self.assert_closed_once(harness)
            harness.opener.open.assert_not_called()
            self.assertEqual(harness.read_results[-1][1], 0)
            self.assertEqual(len(harness.sdk_log.records), 0)
            self.assertEqual([record.getMessage() for record in harness.lambda_log.records],
                             ["lambda_email_receipt_rejected"])

    def test_declared_oversize_rejects_before_read_or_post(self):
        with LoopbackS3(raw_email(), metadata_size=contract.MAX_EMAIL_BYTES + 1) as harness:
            self.assertEqual(harness.invoke(), STOP)
            self.assert_closed_once(harness)
            harness.bodies[0].read.assert_not_called()
            harness.opener.open.assert_not_called()

    def test_max_plus_one_sentinel_is_read_and_rejected_without_post(self):
        data = raw_email(contract.MAX_EMAIL_BYTES + 1)
        with LoopbackS3(data, metadata_size=contract.MAX_EMAIL_BYTES) as harness:
            self.assertEqual(harness.invoke(), STOP)
            self.assert_closed_once(harness)
            harness.opener.open.assert_not_called()
            self.assertEqual(sum(result[1] for result in harness.read_results), contract.MAX_EMAIL_BYTES + 1)
            self.assertEqual(harness.read_results[-1], (1, 1, True))
            self.assertEqual(harness.bodies[0].set_socket_timeout.call_count, contract.MAX_EMAIL_BYTES // CHUNK)
            self.assertEqual(len(harness.sdk_log.records), 0)

    def test_preboundary_setter_failure_has_no_fallback_read_or_post(self):
        with LoopbackS3(raw_email(), setter_failure=True) as harness:
            with self.assertRaises(RuntimeError) as caught:
                harness.invoke()
            self.assertEqual(str(caught.exception), FAILURE)
            self.assertTrue(caught.exception.__suppress_context__)
            self.assert_closed_once(harness)
            harness.bodies[0].set_socket_timeout.assert_called_once()
            harness.bodies[0].read.assert_not_called()
            harness.opener.open.assert_not_called()
            self.assertEqual(len(harness.lambda_log.records), 0)


if __name__ == "__main__":
    unittest.main()
