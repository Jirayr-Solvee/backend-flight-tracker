"""Conservative public ADS-B classifications, not mission or ownership guesses.

Flags/category: https://www.adsbexchange.com/version-2-api/
Cargo is a deliberately small callsign-operator allowlist, not proof of payload.
ICAO operator codes: FAA Order 7340.2 (FDX, UPS, CLX).
"""
import math
import re


def aircraft_metadata(item: dict) -> dict:
    def text(key):
        value = item.get(key)
        return value.strip()[:120] if isinstance(value, str) and value.strip() else None

    flags = item.get("dbFlags")
    # Unknown/malformed is not a verified false or a military classification.
    if isinstance(flags, str) and flags.isdecimal():
        flags = int(flags)
    if type(flags) is not int or flags < 0:
        flags = None
    categories = []
    if flags is not None and flags & 1:
        categories.append("military")
    if flags is not None and flags & 2:
        categories.append("interesting")
    if (text("category") or "").upper() == "A7":
        categories.append("helicopters")
    if re.fullmatch(r"(?:FDX|UPS|CLX)[0-9][A-Z0-9]{0,4}", (text("flight") or "").upper()):
        categories.append("cargo")
    return dict(registration=text("r"), aircraft_type=text("t"),
                aircraft_description=text("desc"), aircraft_categories=categories,
                position_source=text("type"))


def visible_aircraft(position, now_timestamp: int) -> bool:
    """Broad aircraft feed: retain low/slow/registration-only airborne targets."""
    timestamp = position.time_position  # A recent message does not prove a recent position.
    return (not position.on_ground and bool(position.icao24)
            and math.isfinite(position.lat) and -90 <= position.lat <= 90
            and math.isfinite(position.lon) and -180 <= position.lon <= 180
            and timestamp is not None and -30 <= now_timestamp - timestamp <= 300)
