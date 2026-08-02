import base64
import json
import os
import re
import socket
import time
from functools import lru_cache
from typing import Any
from urllib import error, parse, request


BASE_URL = os.getenv("BASE_URL", "https://api.sofly.to").rstrip("/")
GUEST_URL = f"{BASE_URL}/users/me/guest"
DELETE_URL = f"{BASE_URL}/users/me/"
SEARCH_TERM = os.getenv("SEARCH_TERM", "flights from EVN today")
SEARCH_URL = f"{BASE_URL}/flights/search/term?term={parse.quote(SEARCH_TERM)}"
GUEST_SECRET_ID = os.getenv("GUEST_SECRET_ID", "sofly/monitor/guest-key")
TELEGRAM_SECRET_ID = os.getenv("TELEGRAM_SECRET_ID", "sofly/monitor/telegram")
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
BACKOFF_SECONDS = (1, 2)
USER_AGENT = "sofly-aws-health-monitor/1.0"


class MonitorFailure(Exception):
    def __init__(
        self,
        stage: str,
        message: str,
        *,
        status: int | None = None,
        transient: bool = False,
    ):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.status = status
        self.transient = transient


def sanitize_text(value: str | None, *, limit: int = 500) -> str:
    if not value:
        return ""
    text = value.strip()
    text = re.sub(
        r'("(?:jwt|token|access|refresh)"\s*:\s*")([^"]+)(")',
        r"\1<redacted>\3",
        text,
        flags=re.IGNORECASE,
    )
    return text if len(text) <= limit else text[:limit] + "..."


@lru_cache(maxsize=1)
def secrets_client():
    import boto3

    return boto3.client("secretsmanager")


@lru_cache(maxsize=4)
def load_secret(secret_id: str) -> str:
    response = secrets_client().get_secret_value(SecretId=secret_id)
    if "SecretString" in response:
        return response["SecretString"]
    return base64.b64decode(response["SecretBinary"]).decode("utf-8")


def load_guest_key() -> str:
    value = load_secret(GUEST_SECRET_ID).strip()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        value = str(payload.get("guest_key") or "").strip()

    if not value:
        raise MonitorFailure("config", "Guest key secret is empty")
    return value


def load_telegram_config() -> tuple[str, str]:
    try:
        payload = json.loads(load_secret(TELEGRAM_SECRET_ID))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Telegram secret must contain valid JSON") from exc

    bot_token = str(payload.get("bot_token") or "").strip()
    chat_id = str(payload.get("chat_id") or "").strip()
    if not bot_token or not chat_id:
        raise RuntimeError("Telegram secret must contain bot_token and chat_id")
    return bot_token, chat_id


def dns_preflight() -> list[str]:
    try:
        infos = socket.getaddrinfo(
            parse.urlparse(BASE_URL).hostname,
            443,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise MonitorFailure("dns", str(exc), transient=True) from exc

    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise MonitorFailure("dns", "DNS returned no addresses", transient=True)
    return addresses


def http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None = None,
    timeout: int = 30,
) -> tuple[int, str]:
    req = request.Request(url, data=body, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return response.getcode(), response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise MonitorFailure("http", str(exc), transient=True) from exc


def sample_flights(payload: dict[str, Any]) -> tuple[int, int, list[dict[str, str]]]:
    flights = payload.get("flights_result") or []
    airport_flights = payload.get("airport_flights_result") or []
    samples: list[dict[str, str]] = []

    for item in [*flights, *airport_flights]:
        departure = ((item.get("departure") or {}).get("airport") or {}).get("iata")
        arrival = ((item.get("arrival") or {}).get("airport") or {}).get("iata")
        samples.append(
            {
                "flight_number": item.get("number")
                or item.get("flight_number")
                or "unknown",
                "status": str(item.get("status") or "unknown"),
                "route": f"{departure or '?'}->{arrival or '?'}",
            }
        )
        if len(samples) == 3:
            break

    return len(flights), len(airport_flights), samples


def run_monitor() -> dict[str, Any]:
    guest_key = load_guest_key()
    attempts: list[dict[str, Any]] = []

    for attempt_number in range(1, MAX_ATTEMPTS + 1):
        attempt: dict[str, Any] = {
            "attempt": attempt_number,
            "dns_ok": False,
            "dns_addresses": [],
            "guest_status": None,
            "search_status": None,
        }
        jwt: str | None = None

        try:
            attempt["dns_addresses"] = dns_preflight()
            attempt["dns_ok"] = True

            guest_status, guest_body = http_request(
                "POST",
                GUEST_URL,
                headers={
                    "Authorization": f"Bearer {guest_key}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                body=b"{}",
                timeout=30,
            )
            attempt["guest_status"] = guest_status
            if guest_status != 200:
                raise MonitorFailure(
                    "guest_create",
                    f"Guest create returned {guest_status}: {sanitize_text(guest_body)}",
                    status=guest_status,
                    transient=guest_status == 429 or guest_status >= 500,
                )

            try:
                jwt = json.loads(guest_body).get("jwt")
            except json.JSONDecodeError as exc:
                raise MonitorFailure(
                    "guest_create",
                    "Guest create returned invalid JSON",
                ) from exc
            if not jwt:
                raise MonitorFailure(
                    "guest_create",
                    "Guest create response is missing jwt",
                )

            search_status, search_body = http_request(
                "GET",
                SEARCH_URL,
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "User-Agent": USER_AGENT,
                },
                timeout=60,
            )
            attempt["search_status"] = search_status
            if search_status != 200:
                raise MonitorFailure(
                    "search",
                    f"Search returned {search_status}: {sanitize_text(search_body)}",
                    status=search_status,
                    transient=search_status == 429 or search_status >= 500,
                )

            try:
                payload = json.loads(search_body)
            except json.JSONDecodeError as exc:
                raise MonitorFailure("search", "Search returned invalid JSON") from exc

            flights_count, airport_count, samples = sample_flights(payload)
            attempt.update(
                {
                    "flights_result_count": flights_count,
                    "airport_flights_result_count": airport_count,
                    "samples": samples,
                }
            )

            if flights_count == 0 and airport_count == 0:
                raise MonitorFailure(
                    "empty_results",
                    "Search returned zero results for the monitor query",
                    transient=True,
                )

            attempts.append(attempt)
            return {
                "healthy": True,
                "failed_stage": None,
                "retry_count": attempt_number - 1,
                "dns_addresses": attempt["dns_addresses"],
                "guest_status": guest_status,
                "search_status": search_status,
                "flights_result_count": flights_count,
                "airport_flights_result_count": airport_count,
                "samples": samples,
                "attempts": attempts,
            }
        except MonitorFailure as exc:
            attempt["error"] = exc.message
            attempts.append(attempt)
            if exc.transient and attempt_number < MAX_ATTEMPTS:
                backoff_index = min(attempt_number - 1, len(BACKOFF_SECONDS) - 1)
                time.sleep(BACKOFF_SECONDS[backoff_index])
                continue
            return {
                "healthy": False,
                "failed_stage": exc.stage,
                "error": exc.message,
                "retry_count": attempt_number - 1,
                "attempts": attempts,
            }
        finally:
            if jwt:
                try:
                    http_request(
                        "DELETE",
                        DELETE_URL,
                        headers={
                            "Authorization": f"Bearer {jwt}",
                            "User-Agent": USER_AGENT,
                        },
                        timeout=15,
                    )
                except MonitorFailure:
                    pass

    return {
        "healthy": False,
        "failed_stage": "unknown",
        "error": "Monitor finished without a final result",
        "retry_count": max(0, len(attempts) - 1),
        "attempts": attempts,
    }


def send_telegram_message(text: str) -> None:
    bot_token, chat_id = load_telegram_config()
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with request.urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Telegram returned HTTP {exc.code}: {sanitize_text(response_body)}"
        ) from None
    except Exception as exc:
        safe_message = str(exc).replace(bot_token, "<redacted>")
        raise RuntimeError(
            f"Telegram notification failed: {sanitize_text(safe_message)}"
        ) from None

    if not payload.get("ok"):
        raise RuntimeError(
            f"Telegram rejected notification: {sanitize_text(json.dumps(payload))}"
        )


def format_failure(result: dict[str, Any]) -> str:
    attempts = result.get("attempts") or []
    final_attempt = attempts[-1] if attempts else {}
    addresses = final_attempt.get("dns_addresses") or []
    address_text = ", ".join(addresses) if addresses else "none"

    return "\n".join(
        [
            "🚨 Sofly API monitor failed",
            f"Stage: {result.get('failed_stage') or 'unknown'}",
            f"Error: {sanitize_text(str(result.get('error') or 'unknown error'), limit=300)}",
            f"DNS: {address_text}",
            f"Guest status: {final_attempt.get('guest_status')}",
            f"Search status: {final_attempt.get('search_status')}",
            f"Retries: {result.get('retry_count', 0)}",
            f"Query: {SEARCH_TERM}",
            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        ]
    )


def lambda_handler(event, context):
    started_at = time.monotonic()

    if isinstance(event, dict) and event.get("notification_test"):
        send_telegram_message(
            "✅ Sofly API monitor Telegram notification test succeeded."
        )
        return {"healthy": True, "notification_test": True}

    try:
        result = run_monitor()
    except Exception as exc:
        result = {
            "healthy": False,
            "failed_stage": "internal",
            "error": sanitize_text(str(exc)),
            "retry_count": 0,
            "attempts": [],
        }

    result["duration_ms"] = round((time.monotonic() - started_at) * 1000)

    if not result["healthy"]:
        send_telegram_message(format_failure(result))

    print(json.dumps(result, ensure_ascii=True))
    return result
