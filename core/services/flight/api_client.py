import asyncio
from typing import Any

import httpx

from ...config import settings
from ...models.aerodatabox import AerodataboxFlight, AirportFidsContract


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
        self, full_number: str, departure_date: str
    ) -> list[AerodataboxFlight]:
        """Get flights from Aerodatabox API"""
        fetcher_url = f"{settings.AERODATABOX_SERVICE_URL}flights?full_number={full_number}&departure_date={departure_date}"
        response = await self.client.get(fetcher_url)

        if response.status_code != 200:
            return []

        data = response.json()
        return [AerodataboxFlight.model_validate(f) for f in data]

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
