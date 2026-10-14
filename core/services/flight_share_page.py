"""HTML shell and response headers for public flight-share pages.

The shell carries no inline JS and no style attributes. The website's
versioned track script renders the live page from the embedded state block,
and the server-rendered summary keeps the page meaningful without JS.
"""

import html
import json
import logging
import re
from datetime import date, datetime

from ..config import (
    MAPLIBRE_CSS_INTEGRITY,
    MAPLIBRE_CSS_URL,
    MAPLIBRE_JS_INTEGRITY,
    MAPLIBRE_JS_URL,
    settings,
)

SHARE_LANGUAGES = ("en", "de", "es", "fr", "it", "pt-BR", "tr", "ar")
_PRIMARY_LANGUAGES = {
    "en": "en", "de": "de", "es": "es", "fr": "fr",
    "it": "it", "pt": "pt-BR", "tr": "tr", "ar": "ar",
}
RTL_LANGUAGES = frozenset(("ar",))

APP_STORE_APP_ID = "6758215295"
OG_IMAGE_URL = "https://sofly.to/share-assets/og-flight.v1.png"
THEME_COLOR = "#5955f5"

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "script-src 'self' https://cdnjs.cloudflare.com; "
    "style-src 'self' https://cdnjs.cloudflare.com; "
    "img-src 'self' data: blob: https://tiles.openfreemap.org; "
    "connect-src 'self' https://tiles.openfreemap.org; "
    "worker-src blob:; child-src blob:; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# The backend owns these; nginx only proxies. no-referrer keeps the token in
# the page URL out of tile and CDN requests.
PUBLIC_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet, noimageindex",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}

BOT_USER_AGENT = re.compile(
    r"bot|crawler|spider|facebookexternalhit|Facebot|Twitterbot|Slackbot|WhatsApp|"
    r"TelegramBot|Discordbot|LinkedInBot|SkypeUriPreview|Applebot",
    re.IGNORECASE,
)
_PREFETCH_HEADERS = ("sec-purpose", "purpose", "x-purpose", "x-moz")
# Server access logs (uvicorn/gunicorn) record the request path, which for a
# public page is the bearer token itself.
_PUBLIC_SHARE_PATH = re.compile(r"(/public/share/)[^/?#\s\"]+")

_STATUS_LABELS = {
    "Unknown": "Status unknown",
    "Expected": "Scheduled",
    "EnRoute": "En route",
    "CheckIn": "Check-in open",
    "Boarding": "Boarding",
    "GateClosed": "Gate closed",
    "Departed": "Departed",
    "Delayed": "Delayed",
    "Approaching": "Approaching",
    "Arrived": "Landed",
    "Canceled": "Canceled",
    "Diverted": "Diverted",
    "CanceledUncertain": "Canceled",
}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_JSON_ESCAPES = {
    "<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
    " ": "\\u2028", " ": "\\u2029",
}
_UNAVAILABLE_COPY = {
    "not_found": (
        "Link not available · Sofly",
        "This link is no longer available",
        "The person who shared this flight may have stopped sharing it.",
    ),
    "expired": (
        "Flight ended · Sofly",
        "This flight has ended",
        "Live tracking for this flight is over.",
    ),
    "error": (
        "Temporarily unavailable · Sofly",
        "This flight can't be shown right now",
        "Try again in a moment.",
    ),
}


def pick_language(accept_language: str | None) -> str:
    if not accept_language:
        return "en"
    candidates = []
    for index, part in enumerate(accept_language[:512].split(",")[:32]):
        tag, _, parameters = part.strip().partition(";")
        weight = 1.0
        for parameter in parameters.split(";"):
            key, _, value = parameter.strip().partition("=")
            if key.strip().lower() == "q":
                try:
                    weight = float(value.strip())
                except ValueError:
                    weight = 0.0
        # NaN, q=0 and out-of-range weights are not acceptable languages.
        if not 0.0 < weight <= 1.0:
            continue
        language = _PRIMARY_LANGUAGES.get(tag.strip().lower().split("-")[0])
        if language:
            candidates.append((-weight, index, language))
    return min(candidates)[2] if candidates else "en"


def is_bot(user_agent: str | None) -> bool:
    # A request with no user agent is not evidence of a person.
    return not user_agent or BOT_USER_AGENT.search(user_agent[:512]) is not None


def is_prefetch(headers) -> bool:
    # Browser prefetch, prerender and top-sites previews are not a person opening the page.
    return any(
        marker in (headers.get(name) or "")[:128].lower()
        for name in _PREFETCH_HEADERS
        for marker in ("prefetch", "prerender", "preview")
    )


class ShareTokenLogFilter(logging.Filter):
    """Redact the token from server access-log lines for public share paths."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            # uvicorn: '%s - "%s %s HTTP/%s" %d' with the path as an argument.
            record.args = tuple(_redact_share_path(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            # gunicorn: one mapping of access-log atoms. Edit in place to keep
            # its own type, which answers unknown atoms with "-".
            for key, value in list(record.args.items()):
                record.args[key] = _redact_share_path(value)
        elif not record.args and isinstance(record.msg, str):
            record.msg = _redact_share_path(record.msg)
        return True


def _redact_share_path(value):
    return _PUBLIC_SHARE_PATH.sub(r"\1[redacted]", value) if isinstance(value, str) else value


def state_json(state: dict) -> str:
    """JSON safe inside <script type="application/json">; parses back unchanged."""
    text = json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for character, escaped in _JSON_ESCAPES.items():
        text = text.replace(character, escaped)
    return text


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _airport_code(leg: dict) -> str:
    airport = leg.get("airport") or {}
    return airport.get("iata") or airport.get("city") or "—"


def _departure_day(state: dict) -> str | None:
    flight = state["flight"]
    best = flight["departure"].get("scheduled") or flight["departure"].get("best")
    value = None
    if best and best.get("local"):
        value = datetime.fromisoformat(best["local"]).date()
    elif flight.get("date"):
        try:
            value = date.fromisoformat(flight["date"][:10])
        except ValueError:
            value = None
    return f"{value.day} {_MONTHS[value.month - 1]}" if value else None


def _asset(name: str) -> str:
    return f"/share-assets/{name}.v{settings.FLIGHT_SHARE_ASSET_VERSION}"


def _document(*, language: str, page_state: str, head: list[str], body: list[str]) -> str:
    direction = "rtl" if language in RTL_LANGUAGES else "ltr"
    lines = [
        "<!doctype html>",
        f'<html lang="{_e(language)}" dir="{direction}">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
        '<meta name="robots" content="noindex,nofollow,noarchive">',
        '<meta name="referrer" content="no-referrer">',
        f'<meta name="theme-color" content="{THEME_COLOR}">',
        f'<meta name="apple-itunes-app" content="app-id={APP_STORE_APP_ID}">',
        '<link rel="icon" href="/assets/icon.png">',
        *head,
        "</head>",
        f'<body data-state="{_e(page_state)}">',
        f'<main id="app" data-state="{_e(page_state)}">',
        *body,
        "</main>",
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(lines)


def render_active_page(state: dict, language: str) -> str:
    flight = state["flight"]
    number = flight.get("number") or "Flight"
    route = f"{_airport_code(flight['departure'])} → {_airport_code(flight['arrival'])}"
    day = _departure_day(state)
    description = (f"Departs {day} · " if day else "") + "Live status, gates and position on Sofly"
    status = _STATUS_LABELS.get(flight.get("status"), "Status unknown")
    head = [
        f"<title>{_e(number)} · {_e(route)} · Sofly</title>",
        f'<meta name="description" content="{_e(description)}">',
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="Sofly">',
        f'<meta property="og:title" content="Follow {_e(number)} live · {_e(route)}">',
        f'<meta property="og:description" content="{_e(description)}">',
        f'<meta property="og:image" content="{OG_IMAGE_URL}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta name="twitter:card" content="summary_large_image">',
        (
            f'<link rel="stylesheet" href="{_e(MAPLIBRE_CSS_URL)}" '
            f'integrity="{_e(MAPLIBRE_CSS_INTEGRITY)}" crossorigin="anonymous">'
        ),
        f'<link rel="stylesheet" href="{_e(_asset("track"))}.css">',
        f'<script type="application/json" id="sofly-share-state">{state_json(state)}</script>',
        (
            f'<script src="{_e(MAPLIBRE_JS_URL)}" integrity="{_e(MAPLIBRE_JS_INTEGRITY)}" '
            'crossorigin="anonymous" defer></script>'
        ),
        f'<script src="{_e(_asset("track"))}.js" defer></script>',
    ]
    body = [
        '<section class="share-summary" aria-label="Flight summary">',
        f'<h1 class="share-summary-number">{_e(number)}</h1>',
        f'<p class="share-summary-route">{_e(route)}</p>',
        f'<p class="share-summary-status">{_e(status)}</p>',
        "</section>",
        "<noscript><p>Turn on JavaScript to follow this flight live on the map.</p></noscript>",
    ]
    return _document(language=language, page_state="active", head=head, body=body)


def render_unavailable_page(page_state: str, language: str) -> str:
    """The 404/410 (and error) shell: same CSS and script, no map, no flight data."""
    title, heading, detail = _UNAVAILABLE_COPY[page_state]
    head = [
        f"<title>{_e(title)}</title>",
        f'<meta name="description" content="{_e(heading)}">',
        f'<link rel="stylesheet" href="{_e(_asset("track"))}.css">',
        f'<script type="application/json" id="sofly-share-state">{state_json({"state": page_state})}</script>',
        f'<script src="{_e(_asset("track"))}.js" defer></script>',
    ]
    body = [
        '<section class="share-summary">',
        f"<h1>{_e(heading)}</h1>",
        f"<p>{_e(detail)}</p>",
        "</section>",
    ]
    return _document(language=language, page_state=page_state, head=head, body=body)
