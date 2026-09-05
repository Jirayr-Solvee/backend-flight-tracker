"""Bounded, unauthenticated HTTP readiness gate; never imports the application.

Run on the server after a restart. A running systemd unit is not proof that a
Gunicorn worker has booted or can serve requests. This checks local OpenAPI
responses, not provider availability, database health, or every API worker.
"""

import argparse
import json
import subprocess
import time
from dataclasses import dataclass
from typing import Callable


MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    expected_path: str
    expected_method: str


TARGETS = (
    Target("api", "http://127.0.0.1:8000/openapi.json", "/flights/search/term", "post"),
    Target("fetcher", "http://127.0.0.1:8001/openapi.json", "/airport-flights", "get"),
)


@dataclass(frozen=True)
class ProbeResult:
    ready: bool
    reason: str


def probe_target(target: Target, timeout_seconds: float) -> ProbeResult:
    """Hard-bound the entire HTTP operation, including slow response bodies.

    curl is already required by the server's operational smoke checks. Neither
    its stderr nor the response body is printed: diagnostics stay low-cardinality.
    No redirects or proxies are used for these loopback-only production targets.
    """
    try:
        result = subprocess.run(
            [
                "curl", "--silent", "--show-error", "--noproxy", "*",
                "--max-time", str(timeout_seconds),
                "--connect-timeout", str(timeout_seconds),
                "--max-filesize", str(MAX_RESPONSE_BYTES),
                "--write-out", "\n%{http_code}", target.url,
            ],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(False, "timeout")
    except OSError:
        return ProbeResult(False, "curl_unavailable")

    if result.returncode:
        return ProbeResult(False, "timeout" if result.returncode == 28 else "http_transport_error")

    body, separator, status = result.stdout.rpartition(b"\n")
    if not separator or status != b"200":
        reason = f"http_{status.decode('ascii')}" if len(status) == 3 and status.isdigit() else "invalid_http_response"
        return ProbeResult(False, reason)
    if len(body) > MAX_RESPONSE_BYTES:
        return ProbeResult(False, "response_too_large")
    try:
        document = json.loads(body)
    except (ValueError, UnicodeError):
        return ProbeResult(False, "invalid_openapi_json")

    if not isinstance(document, dict) or not str(document.get("openapi", "")).startswith("3."):
        return ProbeResult(False, "invalid_openapi_document")
    paths = document.get("paths")
    operation = paths.get(target.expected_path) if isinstance(paths, dict) else None
    if not isinstance(operation, dict) or not isinstance(operation.get(target.expected_method), dict):
        return ProbeResult(False, "wrong_service")
    return ProbeResult(True, "http_200_validated")


def check_readiness(
    targets: tuple[Target, ...] = TARGETS,
    *,
    deadline_seconds: float = 45.0,
    request_timeout_seconds: float = 3.0,
    poll_interval_seconds: float = 1.0,
    probe: Callable[[Target, float], ProbeResult] = probe_target,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    report: Callable[[str], None] = print,
) -> bool:
    if not targets or min(deadline_seconds, request_timeout_seconds, poll_interval_seconds) <= 0:
        raise ValueError("Targets and positive timeout/polling values are required")

    deadline = clock() + deadline_seconds
    consecutive = {target.name: 0 for target in targets}
    last_reason = {target.name: "not_checked" for target in targets}
    while clock() < deadline:
        for target in targets:
            remaining = deadline - clock()
            if remaining <= 0:
                break
            result = probe(target, min(request_timeout_seconds, remaining))
            if clock() > deadline:
                result = ProbeResult(False, "deadline_exceeded")
            consecutive[target.name] = consecutive[target.name] + 1 if result.ready else 0
            last_reason[target.name] = result.reason

        if clock() <= deadline and all(count >= 2 for count in consecutive.values()):
            report("READY " + ", ".join(target.name for target in targets)
                   + ": two consecutive validated HTTP 200 responses per service.")
            return True
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(poll_interval_seconds, remaining))

    report("NOT READY: " + "; ".join(
        f"{target.name}={last_reason[target.name]} (consecutive={consecutive[target.name]}/2)"
        for target in targets
    ))
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=("all", "api", "fetcher"), default="all")
    args = parser.parse_args()
    targets = tuple(target for target in TARGETS if args.service in ("all", target.name))
    return 0 if check_readiness(targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())
