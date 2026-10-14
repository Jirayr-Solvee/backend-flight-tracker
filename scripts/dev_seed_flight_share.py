#!/usr/bin/env python3
"""Seed a throwaway SQLite database with shared sample flights for local preview.

Local development only. The script works in a new scratch directory (core.models
opens ./database.db relative to the working directory), replaces every setting
with an inert placeholder before importing core, and never calls a provider.
The printed tokens only resolve against this throwaway database.

    .venv/bin/python scripts/dev_seed_flight_share.py            # seed and print tokens
    .venv/bin/python scripts/dev_seed_flight_share.py --serve    # then serve 127.0.0.1:8000

Point the website's tools/share_preview_server.py at the printed backend URL to
open the pages as https://sofly.to/f/<token> would render them.
"""

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
# Overrides, not defaults: a developer shell may export real credentials, and
# tokens or provider clients must never be derived from them here.
INERT_SETTINGS = {
    "AWS_ACCESS_KEY_ID": "dev", "AWS_SECRET_ACCESS_KEY": "dev", "AWS_BUCKET_NAME": "dev",
    "AWS_REGION": "eu-north-1", "AWS_EC2_METADATA_DISABLED": "true",
    "LAMBDA_FUNCTION_AUTH_TOKEN": "dev", "GEMINI_API_KEY": "dev",
    "API_URL": "https://example.invalid", "JWT_SECRET": "dev-flight-share-preview-only",
    "JWT_ALGORITHM": "HS256", "JWT_EXPIRE_DAYS": "1", "KEY_ID": "dev", "ISSUER_ID": "dev",
    "BUNDLE_ID": "com.zhirayr.Flight-tracker-shared", "APP_APPLE_ID": "1", "TEAM_ID": "dev",
    "X_API_MARKET_KEY": "dev", "AERODATABOX_SERVICE_URL": "https://example.invalid",
    "ADSBEXCHANGE_API_KEY": "", "BALANCE_REFILL_AMMOUNT": "1", "BALANCE_REFILL_THRESHOLD": "1",
    "DEV_ENV": "true", "JWS_ENV": "XCODE", "MAX_PREMIUM_HOURS": "1", "APPLE_ISSUER": "dev",
    "APPLE_KEYS_URL": "https://example.invalid", "GUEST_KEY": "dev",
    "APN_KEY_PATH": str(Path(__file__).resolve()), "APPLE_ROOT_CERT_PATH": str(Path(__file__).resolve()),
    "AIRLINE_MAP_JSON": str(REPOSITORY / "iata_to_icao.json"),
}
OWNER_ID = "00000000-0000-4000-8000-00000000d3f0"  # synthetic local owner

# (iata, name, city, country, lat, lon, preview UTC offset). Offsets are fixed
# for October; real rows carry the provider's offset per time instead.
AIRPORTS = {
    "LHR": ("London Heathrow", "London", "GB", 51.4706, -0.461941, 60),
    "JFK": ("John F Kennedy International", "New York", "US", 40.6398, -73.7789, -240),
    "LAX": ("Los Angeles International", "Los Angeles", "US", 33.9425, -118.408, -420),
    "FRA": ("Frankfurt am Main", "Frankfurt", "DE", 50.0333, 8.5706, 120),
    "DXB": ("Dubai International", "Dubai", "AE", 25.2528, 55.3644, 240),
    "IST": ("Istanbul", "Istanbul", "TR", 41.2753, 28.7519, 180),
    "GRU": ("São Paulo Guarulhos", "São Paulo", "BR", -23.4356, -46.4731, -180),
    "SYD": ("Sydney Kingsford Smith", "Sydney", "AU", -33.9461, 151.177, 660),
}
# label, expected HTTP status, number, airline, status, from, to,
# departure offsets and arrival offsets in minutes from now: (kind, minutes).
SAMPLES = (
    ("pre_departure", 200, "BA178", ("British Airways", "BA", "BAW"), "Expected", "LHR", "JFK",
     (("scheduled", 125),), (("scheduled", 125 + 475),)),
    ("airborne_delayed", 200, "AA100", ("American Airlines", "AA", "AAL"), "EnRoute", "JFK", "LAX",
     (("scheduled", -200), ("runway", -175)), (("scheduled", 160), ("predicted", 185))),
    ("airborne_long_haul", 200, "QF2", ("Qantas", "QF", "QFA"), "EnRoute", "SYD", "LHR",
     (("scheduled", -600), ("runway", -590)), (("scheduled", 790), ("predicted", 780))),
    ("landed", 200, "LH400", ("Lufthansa", "LH", "DLH"), "Arrived", "FRA", "JFK",
     (("scheduled", -540), ("runway", -535)), (("scheduled", -20), ("runway", -25))),
    ("canceled", 200, "EK1", ("Emirates", "EK", "UAE"), "Canceled", "DXB", "LHR",
     (("scheduled", 60),), (("scheduled", 60 + 470),)),
    ("diverted", 200, "TK1", ("Turkish Airlines", "TK", "THY"), "Diverted", "IST", "LHR",
     (("scheduled", -300), ("runway", -290)), (("scheduled", -30),)),
    ("no_coordinates", 200, "LA8084", ("LATAM", "LA", "LAN"), "EnRoute", "GRU", None,
     (("scheduled", -120), ("runway", -110)), (("scheduled", 240),)),
    ("expired", 410, "BA117", ("British Airways", "BA", "BAW"), "Arrived", "LHR", "JFK",
     (("scheduled", -900),), (("scheduled", -420), ("runway", -430))),
    ("revoked", 404, "BA283", ("British Airways", "BA", "BAW"), "Expected", "LHR", "LAX",
     (("scheduled", 240),), (("scheduled", 240 + 660),)),
)


def _prepare(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "database.db").exists():
        raise SystemExit("Refusing to reuse an existing database.db. Pass a new, empty --dir.")
    os.environ.update(INERT_SETTINGS)
    os.chdir(directory)
    sys.path.insert(0, str(REPOSITORY))


def _provider_times(kind_offsets, offset_minutes: int | None, now: datetime) -> dict:
    # Stored exactly like the provider mapper writes them, so normalisation runs.
    fields = {}
    for kind, minutes in kind_offsets:
        moment = now + timedelta(minutes=minutes)
        fields[f"{kind}_time_utc"] = moment.strftime("%Y-%m-%d %H:%MZ")
        if offset_minutes is not None:
            local = moment + timedelta(minutes=offset_minutes)
            sign = "+" if offset_minutes >= 0 else "-"
            hours, minutes_part = divmod(abs(offset_minutes), 60)
            fields[f"{kind}_time_local"] = local.strftime("%Y-%m-%d %H:%M") + f"{sign}{hours:02d}:{minutes_part:02d}"
    return fields


def seed(directory: Path) -> list[tuple[str, int, str]]:
    from sqlmodel import Session

    from core.models import engine
    from core.models.flight import Airline, Airport, Arrival, Departure, Flight
    from core.models.user import User, UserFlightLink
    from core.services import flight_share as share_service

    def airport(code):
        if code is None:
            # The airport-search path's placeholder row: no name, no location.
            return Airport(iata="", name="unknown", municipality_name="", lat=0.0, lon=0.0, country_code=""), None
        name, city, country, lat, lon, offset = AIRPORTS[code]
        return Airport(iata=code, name=name, municipality_name=city, lat=lat, lon=lon, country_code=country), offset

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    results = []
    with Session(engine) as session:
        session.add(User(id=OWNER_ID, verified=False))
        session.commit()
        for label, expected, number, airline, status, origin, destination, departure, arrival in SAMPLES:
            origin_airport, origin_offset = airport(origin)
            destination_airport, destination_offset = airport(destination)
            departure_fields = _provider_times(departure, origin_offset, now)
            flight = Flight(
                date=departure_fields["scheduled_time_local"][:10] if origin_offset is not None
                else departure_fields["scheduled_time_utc"][:10],
                number=number, status=status, distance_km=None,
                aircraft_model="Airbus A350-1000" if number == "QF2" else "Boeing 777-300ER",
                aircraft_reg="VH-ZNA" if number == "QF2" else None,
                airline=Airline(name=airline[0], iata=airline[1], icao=airline[2]),
                departure=Departure(airport=origin_airport, terminal="5", gate="B32", **departure_fields),
                arrival=Arrival(
                    airport=destination_airport, terminal="4", baggage_belt="7",
                    **_provider_times(arrival, destination_offset, now),
                ),
            )
            session.add(flight)
            session.commit()
            session.add(UserFlightLink(user_id=OWNER_ID, flight_id=flight.id))
            session.commit()
            share, _ = share_service.create_or_reuse_share(session, OWNER_ID, flight.id)
            token = share_service.share_token(share.id)
            if label == "revoked":
                share_service.revoke_flight_shares(session, OWNER_ID, flight.id, "owner")
                session.commit()
            results.append((label, expected, token))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, help="new scratch directory (default: a fresh temp dir)")
    parser.add_argument("--serve", action="store_true", help="serve the API on 127.0.0.1 after seeding")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    directory = (args.dir or Path(tempfile.mkdtemp(prefix="sofly-flight-share-dev-"))).resolve()
    _prepare(directory)
    results = seed(directory)
    backend = f"http://127.0.0.1:{args.port}"
    print(f"Throwaway database: {directory / 'database.db'} (local preview only)")
    print(f"{'sample':<20} {'status':<7} token / backend page")
    for label, expected, token in results:
        print(f"{label:<20} {expected:<7} {token}  {backend}/public/share/{token}")
    print(f"Preview server: proxy /f/<token> to {backend} (website tools/share_preview_server.py).", flush=True)
    if not args.serve:
        print("Serve later by re-running with --serve (tokens change on every seed).")
        return
    import uvicorn

    from core.main import app

    # Loopback only: inert settings, throwaway data, no provider credentials.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
