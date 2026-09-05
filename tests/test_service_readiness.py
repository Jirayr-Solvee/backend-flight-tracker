import json
import shutil
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from scripts.check_service_readiness import (
    TARGETS,
    ProbeResult,
    Target,
    check_readiness,
    probe_target,
)


def openapi_body(target):
    return json.dumps({"openapi": "3.1.0", "paths": {
        target.expected_path: {target.expected_method: {}}
    }}).encode()


class ProbeTests(unittest.TestCase):
    @mock.patch("scripts.check_service_readiness.subprocess.run")
    def test_requires_exact_success_and_correct_service_identity(self, run):
        for target in TARGETS:
            with self.subTest(target=target.name):
                run.return_value = subprocess.CompletedProcess([], 0, openapi_body(target) + b"\n200")
                self.assertTrue(probe_target(target, 3).ready)
                self.assertEqual(run.call_args.kwargs["timeout"], 3)
        run.return_value = subprocess.CompletedProcess([], 0, openapi_body(TARGETS[0]) + b"\n200")
        self.assertEqual(probe_target(TARGETS[1], 3).reason, "wrong_service")

    @mock.patch("scripts.check_service_readiness.subprocess.run")
    def test_http_errors_redirects_and_invalid_success_bodies_fail(self, run):
        for stdout, expected in (
            (b"private server error\n503", "http_503"),
            (b"redirect\n302", "http_302"),
            (b"<html>wrong page</html>\n200", "invalid_openapi_json"),
            (b"[]\n200", "invalid_openapi_document"),
            (b'{"openapi":"3.1.0","paths":[]}\n200', "wrong_service"),
        ):
            with self.subTest(expected=expected):
                run.return_value = subprocess.CompletedProcess([], 0, stdout)
                self.assertEqual(probe_target(TARGETS[0], 3).reason, expected)

    @mock.patch("scripts.check_service_readiness.subprocess.run")
    def test_hung_or_missing_curl_is_a_bounded_failure(self, run):
        run.side_effect = subprocess.TimeoutExpired("curl", 0.1)
        self.assertEqual(probe_target(TARGETS[0], 0.1).reason, "timeout")
        run.side_effect = FileNotFoundError()
        self.assertEqual(probe_target(TARGETS[0], 0.1).reason, "curl_unavailable")
        run.side_effect = None
        run.return_value = subprocess.CompletedProcess([], 28, b"", b"private diagnostic")
        self.assertEqual(probe_target(TARGETS[0], 0.1).reason, "timeout")


class GateTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.reports = []

    def sleep(self, duration):
        self.now += duration

    def gate(self, probe, **kwargs):
        return check_readiness(probe=probe, clock=lambda: self.now, sleep=self.sleep,
                               report=self.reports.append, **kwargs)

    def test_both_services_need_two_passes_not_merely_one(self):
        calls = []
        def probe(target, timeout):
            calls.append(target.name)
            return ProbeResult(True, "http_200_validated")
        self.assertTrue(self.gate(probe))
        self.assertEqual(calls, ["api", "fetcher", "api", "fetcher"])
        self.assertEqual(self.now, 1)

    def test_one_unresponsive_service_cannot_be_reported_healthy(self):
        calls = []
        def probe(target, timeout):
            calls.append((target.name, timeout))
            self.now += timeout
            return ProbeResult(target.name == "api", "timeout" if target.name == "fetcher" else "http_200_validated")
        self.assertFalse(self.gate(probe, deadline_seconds=8))
        self.assertLessEqual(self.now, 8)
        self.assertTrue(all(timeout <= 3 for _, timeout in calls))
        self.assertEqual(calls[-1], ("api", 1))
        self.assertIn("fetcher=timeout", self.reports[-1])

    def test_failure_resets_streak_and_recovery_before_deadline_passes(self):
        statuses = iter((True, False, True, True))
        calls = []
        def probe(target, timeout):
            calls.append(target.name)
            ready = next(statuses) if target.name == "fetcher" else True
            return ProbeResult(ready, "http_200_validated" if ready else "http_503")
        self.assertTrue(self.gate(probe))
        self.assertEqual(calls.count("fetcher"), 4)
        self.assertEqual(self.now, 3)

    def test_success_after_deadline_is_rejected(self):
        def probe(target, timeout):
            self.now += 5
            return ProbeResult(True, "http_200_validated")
        self.assertFalse(self.gate(probe, deadline_seconds=4))
        self.assertIn("deadline_exceeded", self.reports[-1])

    def test_single_service_gate_and_no_response_body_in_output(self):
        self.assertTrue(self.gate(lambda *_: ProbeResult(True, "http_200_validated"), targets=(TARGETS[1],)))
        self.assertEqual(len(self.reports), 1)
        self.assertIn("READY fetcher", self.reports[0])
        self.assertNotIn("http://", self.reports[0])


@unittest.skipUnless(shutil.which("curl"), "curl is required on the deployment host")
class LocalHTTPTests(unittest.TestCase):
    def test_real_http_identity_and_read_timeout_without_external_network(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/slow":
                    time.sleep(0.4)
                body = openapi_body(TARGETS[1])
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            target = Target("fetcher", base_url + "/openapi.json", "/airport-flights", "get")
            self.assertTrue(probe_target(target, 2).ready)
            target = Target("fetcher", base_url + "/slow", "/airport-flights", "get")
            started = time.monotonic()
            self.assertEqual(probe_target(target, 0.1).reason, "timeout")
            self.assertLess(time.monotonic() - started, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
