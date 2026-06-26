import asyncio
import logging
from typing import Any

import httpx
from pydantic import ValidationError

from ...config import settings
from ...models.aerodatabox import AerodataboxFlight, AirportFidsContract
from ...models.flight import GlobalFlightPositionRead

logger = logging.getLogger(__name__)


class AerodataboxClient:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,
                read=20.0,
                write=5.0,
                pool=5.0,
            )
        )

    async def get_flight(
        self, full_number: str, departure_date: str, with_location: bool = False
    ) -> list[AerodataboxFlight]:
        """Get flights from Aerodatabox API"""
        fetcher_url = (
            f"{settings.AERODATABOX_SERVICE_URL}"
            f"flights?full_number={full_number}&departure_date={departure_date}"
            f"&with_location={str(with_location).lower()}"
        )
        response = await self.client.get(fetcher_url)

        if response.status_code != 200:
            return []

        data = response.json()
        if not isinstance(data, list):
            return []

        flights: list[AerodataboxFlight] = []
        for item in data:
            try:
                flights.append(AerodataboxFlight.model_validate(item))
            except ValidationError as exc:
                logger.warning(
                    "Skipping invalid Aerodatabox flight full_number=%s departure_date=%s error=%s",
                    full_number,
                    departure_date,
                    exc.errors(include_url=False),
                )

        return flights

    async def get_airport_flights(
        self, airport_iata: str, departure_date: str, direction: str = "Both"
    ) -> AirportFidsContract:
        """
        Get flights for an airport for the window of 24H from Aerodatabox API.
        """
        airport_iata = airport_iata.strip().upper()

        timewindows = ["morning", "afternoon"]

        fetcher_url = f"{settings.AERODATABOX_SERVICE_URL}airport-flights?airport_iata={airport_iata}&departure_date={departure_date}&direction={direction}"

        r1, r2 = await asyncio.gather(
            self.client.get(fetcher_url + f"&time_window={timewindows[0]}"),
            self.client.get(fetcher_url + f"&time_window={timewindows[1]}"),
        )

        dep_1 = AirportFidsContract()
        dep_2 = AirportFidsContract()

        if r1.status_code == 200:
            dep_1 = AirportFidsContract.model_validate(r1.json())
        
        if r2.status_code == 200:
            dep_2 = AirportFidsContract.model_validate(r2.json())

        combined_departures = (dep_1.departures or []) + (dep_2.departures or [])
        combined_arrivals = (dep_1.arrivals or []) + (dep_2.arrivals or [])
        return AirportFidsContract(
            departures=combined_departures, arrivals=combined_arrivals
        )

    async def get_flight_delays(self, full_number: str) -> dict[str, Any]:
        full_number = full_number.strip().replace(" ", "").upper()
        fetcher_url = (
            f"{settings.AERODATABOX_SERVICE_URL}"
            f"flight-delays?full_number={full_number}"
        )
        response = await self.client.get(fetcher_url)

        if response.status_code != 200:
            return {}

        return response.json()

    async def get_airport_delay(
        self, airport_iata: str | None, date_local: str | None = None
    ) -> dict[str, Any]:
        if not airport_iata:
            return {}

        airport_iata = airport_iata.strip().upper()
        fetcher_url = (
            f"{settings.AERODATABOX_SERVICE_URL}"
            f"airport-delay?airport_iata={airport_iata}"
        )
        if date_local:
            fetcher_url = f"{fetcher_url}&date_local={date_local}"

        response = await self.client.get(fetcher_url)

        if response.status_code != 200:
            return {}

        return response.json()


class ADSBExchangeClient:
    FEET_TO_METERS = 0.3048
    KNOTS_TO_MPS = 0.514444
    FPM_TO_MPS = 0.00508

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,
                read=20.0,
                write=5.0,
                pool=5.0,
            )
        )

    async def get_global_positions(self) -> list[GlobalFlightPositionRead]:
        api_key = (settings.ADSBEXCHANGE_API_KEY or "").strip()
        if not api_key:
            return []

        base_url = settings.ADSBEXCHANGE_BASE_URL.rstrip("/")
        responses = await asyncio.gather(
            *[
                self.client.get(
                    f"{base_url}/{path}",
                    headers=self._headers(api_key),
                )
                for path in self._global_paths()
            ],
            return_exceptions=True,
        )

        positions_by_id: dict[str, GlobalFlightPositionRead] = {}
        for response in responses:
            if isinstance(response, Exception) or response.status_code != 200:
                continue

            for position in self._positions_from_payload(response.json()):
                positions_by_id[position.icao24] = position

        return list(positions_by_id.values())

    @staticmethod
    def _global_paths() -> list[str]:
        paths = []
        for path in settings.ADSBEXCHANGE_GLOBAL_PATHS.split("|"):
            normalized = path.strip().strip("/")
            if normalized:
                paths.append(normalized)

        return paths or ["all"]

    @classmethod
    def _positions_from_payload(
        cls, payload: dict[str, Any]
    ) -> list[GlobalFlightPositionRead]:
        aircraft = payload.get("ac") or payload.get("aircraft")
        if aircraft is None and payload.get("hex"):
            aircraft = [payload]

        if not isinstance(aircraft, list):
            return []

        now_ms = cls._float_value(payload.get("now"))
        positions: list[GlobalFlightPositionRead] = []
        for item in aircraft:
            if not isinstance(item, dict):
                continue

            position = cls._position_from_aircraft(item=item, now_ms=now_ms)
            if position:
                positions.append(position)

        return positions

    async def get_positions_for_path(self, path: str) -> list[GlobalFlightPositionRead]:
        api_key = (settings.ADSBEXCHANGE_API_KEY or "").strip()
        if not api_key:
            return []

        base_url = settings.ADSBEXCHANGE_BASE_URL.rstrip("/")
        response = await self.client.get(
            f"{base_url}/{path.strip().strip('/')}",
            headers=self._headers(api_key),
        )
        if response.status_code != 200:
            return []

        return self._positions_from_payload(response.json())

    def _headers(self, api_key: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            settings.ADSBEXCHANGE_AUTH_HEADER: api_key,
        }
        rapidapi_host = (settings.ADSBEXCHANGE_RAPIDAPI_HOST or "").strip()
        if rapidapi_host:
            headers["x-rapidapi-host"] = rapidapi_host
        return headers

    @classmethod
    def _position_from_aircraft(
        cls, item: dict[str, Any], now_ms: float | None
    ) -> GlobalFlightPositionRead | None:
        icao24 = cls._string_value(item.get("hex"))
        if not icao24 or icao24.startswith("~"):
            return None

        lat = cls._float_value(item.get("lat"))
        lon = cls._float_value(item.get("lon"))
        if lat is None or lon is None:
            return None

        on_ground = cls._is_on_ground(item)
        if on_ground:
            return None

        altitude_feet = cls._altitude_feet(item.get("alt_geom"))
        if altitude_feet is None:
            altitude_feet = cls._altitude_feet(item.get("alt_baro"))

        baro_altitude_feet = cls._altitude_feet(item.get("alt_baro"))
        geo_altitude_feet = cls._altitude_feet(item.get("alt_geom"))
        ground_speed_kt = cls._float_value(item.get("gs"))
        vertical_speed_fpm = cls._vertical_speed_fpm(item)
        callsign = cls._string_value(item.get("flight"))

        return GlobalFlightPositionRead(
            id=icao24.lower(),
            icao24=icao24.lower(),
            callsign=callsign,
            display_code=callsign or icao24.upper(),
            origin_country=None,
            time_position=cls._seen_timestamp(now_ms, item.get("seen_pos")),
            last_contact=cls._seen_timestamp(now_ms, item.get("seen")),
            lat=lat,
            lon=lon,
            baro_altitude_m=(
                baro_altitude_feet * cls.FEET_TO_METERS
                if baro_altitude_feet is not None
                else None
            ),
            geo_altitude_m=(
                geo_altitude_feet * cls.FEET_TO_METERS
                if geo_altitude_feet is not None
                else None
            ),
            altitude_feet=altitude_feet,
            velocity_mps=(
                ground_speed_kt * cls.KNOTS_TO_MPS
                if ground_speed_kt is not None
                else None
            ),
            ground_speed_kt=ground_speed_kt,
            true_track_deg=cls._float_value(item.get("track")),
            vertical_rate_mps=(
                vertical_speed_fpm * cls.FPM_TO_MPS
                if vertical_speed_fpm is not None
                else None
            ),
            vertical_speed_fpm=vertical_speed_fpm,
            on_ground=on_ground,
        )

    @staticmethod
    def _string_value(value: Any) -> str | None:
        if value is None:
            return None

        result = str(value).strip()
        return result or None

    @staticmethod
    def _float_value(value: Any) -> float | None:
        if value is None:
            return None

        if isinstance(value, str) and value.strip().lower() in {"", "ground"}:
            return None

        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _altitude_feet(cls, value: Any) -> float | None:
        return cls._float_value(value)

    @classmethod
    def _vertical_speed_fpm(cls, item: dict[str, Any]) -> int | None:
        value = cls._float_value(item.get("geom_rate"))
        if value is None:
            value = cls._float_value(item.get("baro_rate"))
        return round(value) if value is not None else None

    @classmethod
    def _is_on_ground(cls, item: dict[str, Any]) -> bool:
        ground_value = item.get("gnd")
        if isinstance(ground_value, bool):
            return ground_value

        altitude = item.get("alt_baro")
        return isinstance(altitude, str) and altitude.strip().lower() == "ground"

    @classmethod
    def _seen_timestamp(cls, now_ms: float | None, seen_seconds: Any) -> int | None:
        if now_ms is None:
            return None

        seen = cls._float_value(seen_seconds)
        if seen is None:
            return int(now_ms / 1000)

        return int((now_ms / 1000) - seen)


class OpenSkyClient:
    STATES_ALL_URL = "https://opensky-network.org/api/states/all"
    METERS_TO_FEET = 3.28084
    MPS_TO_KNOTS = 1.943844
    MPS_TO_FPM = 196.850394

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,
                read=20.0,
                write=5.0,
                pool=5.0,
            )
        )

    async def get_global_positions(self) -> list[GlobalFlightPositionRead]:
        response = await self.client.get(self.STATES_ALL_URL)
        if response.status_code != 200:
            return []

        data = response.json()
        states = data.get("states") or []
        positions: list[GlobalFlightPositionRead] = []

        for state in states:
            if not isinstance(state, list) or len(state) < 11:
                continue

            icao24 = self._string_at(state, 0)
            callsign = self._string_at(state, 1)
            lon = self._float_at(state, 5)
            lat = self._float_at(state, 6)
            on_ground = bool(state[8])

            if not icao24 or lat is None or lon is None or on_ground:
                continue

            baro_altitude_m = self._float_at(state, 7)
            geo_altitude_m = self._float_at(state, 13) if len(state) > 13 else None
            altitude_m = geo_altitude_m if geo_altitude_m is not None else baro_altitude_m
            velocity_mps = self._float_at(state, 9)
            vertical_rate_mps = self._float_at(state, 11) if len(state) > 11 else None

            positions.append(
                GlobalFlightPositionRead(
                    id=icao24,
                    icao24=icao24,
                    callsign=callsign,
                    display_code=callsign or icao24.upper(),
                    origin_country=self._string_at(state, 2),
                    time_position=self._int_at(state, 3),
                    last_contact=self._int_at(state, 4),
                    lat=lat,
                    lon=lon,
                    baro_altitude_m=baro_altitude_m,
                    geo_altitude_m=geo_altitude_m,
                    altitude_feet=(
                        altitude_m * self.METERS_TO_FEET
                        if altitude_m is not None
                        else None
                    ),
                    velocity_mps=velocity_mps,
                    ground_speed_kt=(
                        velocity_mps * self.MPS_TO_KNOTS
                        if velocity_mps is not None
                        else None
                    ),
                    true_track_deg=self._float_at(state, 10),
                    vertical_rate_mps=vertical_rate_mps,
                    vertical_speed_fpm=(
                        round(vertical_rate_mps * self.MPS_TO_FPM)
                        if vertical_rate_mps is not None
                        else None
                    ),
                    on_ground=on_ground,
                )
            )

        return positions

    @staticmethod
    def _string_at(values: list, index: int) -> str | None:
        if index >= len(values) or values[index] is None:
            return None

        value = str(values[index]).strip()
        return value or None

    @staticmethod
    def _float_at(values: list, index: int) -> float | None:
        if index >= len(values) or values[index] is None:
            return None

        try:
            return float(values[index])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int_at(values: list, index: int) -> int | None:
        if index >= len(values) or values[index] is None:
            return None

        try:
            return int(values[index])
        except (TypeError, ValueError):
            return None
