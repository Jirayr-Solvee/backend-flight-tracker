import asyncio

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

        if r1.status_code != 200 or r2.status_code != 200:
            return AirportFidsContract()

        dep1 = AirportFidsContract.model_validate(r1.json())
        dep2 = AirportFidsContract.model_validate(r2.json())

        combined_departures = (dep1.departures or []) + (dep2.departures or [])
        combined_arrivals = (dep1.arrivals or []) + (dep2.arrivals or [])
        return AirportFidsContract(
            departures=combined_departures, arrivals=combined_arrivals
        )
