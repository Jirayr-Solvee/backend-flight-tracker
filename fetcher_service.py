import logging
from typing import Any, Literal

import httpx
from aiolimiter import AsyncLimiter
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel

from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)

logger = logging.getLogger()


app = FastAPI()


class BalanceResponse(BaseModel):
    creditsRemaining: int


class AerodataboxFetcherService:
    def __init__(self):
        self.limiter = AsyncLimiter(max_rate=1, time_period=1)

        self.base_url = "https://prod.api.market"
        self.client = httpx.AsyncClient(
            headers={"X-Api-Market-Key": settings.X_API_MARKET_KEY},
            timeout=httpx.Timeout(
                connect=5.0,
                read=20.0,
                write=5.0,
                pool=5.0,
            ),
        )

    async def fetch_single_flight(self, full_number: str, departure_date: str) -> Any:
        async with self.limiter:
            try:
                url = f"{self.base_url}/api/v1/aedbx/aerodatabox/flights/Number/{full_number}/{departure_date}?dateLocalRole=Departure&withAircraftImage=false&withLocation=false&withFlightPlan=false"
                response = await self.client.get(url)

                if response.status_code != 200:
                    logger.warning(
                        f"Aerodatabox responded with status={response.status_code} for full_number={full_number}, departure_date={departure_date}"
                    )
                    raise HTTPException(status_code=response.status_code)

                return response.json()
            except HTTPException:
                raise
            except Exception:
                logger.exception(
                    f"Error while fetching flights for flight_number={full_number}, departure_date={departure_date}"
                )
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    async def fetch_airport_flights(
        self,
        airport_iata: str,
        departure_date: str,
        time_window: Literal["morning", "afternoon"],
        direction: str = "Both",
    ) -> Any:
        async with self.limiter:
            try:
                url_base = (
                    f"{self.base_url}/api/v1/aedbx/aerodatabox/flights/airports/Iata"
                )

                airport_iata = airport_iata.strip().upper()

                if time_window == "morning":
                    url = f"{url_base}/{airport_iata}/{departure_date}T00%3A00/{departure_date}T12%3A00"
                else:
                    url = f"{url_base}/{airport_iata}/{departure_date}T12%3A00/{departure_date}T23%3A59"

                params = (
                    f"?direction={direction}&withLeg=true&withCancelled=true&"
                    "withCodeshared=true&withCargo=false&withPrivate=true&withLocation=false"
                )

                print("===========")
                print(url + params)
                print("===========")

                response = await self.client.get(url + params)

                if response.status_code != 200:
                    logger.exception(
                        f"Aerodatabox responsded with status={response.status_code}, airport_iata={airport_iata}, departure_date={departure_date}, time_window={time_window}, direction={direction}"
                    )
                    raise HTTPException(status_code=response.status_code)

                return response.json()
            except HTTPException:
                raise
            except Exception:
                logger.exception(
                    f"Error while fetching airport flights, airport_iata={airport_iata}, departure_date={departure_date}, time_window={time_window}, direction={direction}"
                )
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    async def create_webhook(self, flight_full_number: str) -> Any:
        url = f"{self.base_url}/api/v1/aedbx/aerodatabox/subscriptions/webhook/FlightByNumber/{flight_full_number}?useCredits=true"
        payload = {
            "url": f"{settings.API_URL}/webhook/aerodatabox",
            "maxDeliveryRetries": 1,
        }

        async with self.limiter:
            try:
                response = await self.client.post(url=url, json=payload)
                if response.status_code != 200:
                    logger.warning(
                        f"Aerodatabox responded with status code={response.status_code} while creating a webhook sub for flight_full_number={flight_full_number}"
                    )
                    raise HTTPException(status_code=response.status_code)

                return response.json()
            except HTTPException:
                raise
            except Exception:
                logger.exception(
                    f"Error during creation of webhook subscription for flight_full_number={flight_full_number}"
                )
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    async def delete_webhook(self, subscription_id: str) -> bool:
        """
        Delete webhook subscription
        """
        async with self.limiter:
            url = f"{self.base_url}/api/v1/aedbx/aerodatabox/subscriptions/webhook/{subscription_id}"

            try:
                response = await self.client.delete(url)
                if response.status_code != 200:
                    logger.warning(
                        f"Aerodatabox responded with status code={response.status_code} while deleting a webhook sub for subscription_id={subscription_id}"
                    )
                    raise HTTPException(status_code=response.status_code)

                raise HTTPException(status_code=status.HTTP_200_OK)
            except HTTPException:
                raise
            except Exception:
                logger.exception(
                    f"Error during deletion of webhook subscription_id={subscription_id}"
                )
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    async def is_subscription_balance_low(self) -> bool:
        async with self.limiter:
            try:
                url = f"{self.base_url}/api/v1/aedbx/aerodatabox/subscriptions/balance"
                response = await self.client.get(url)

                if response.status_code != 200:
                    logger.warning(
                        f"Aerodatabox responded with status code={response.status_code} while checking subscription balance"
                    )
                    raise HTTPException(status_code=response.status_code)

                data = BalanceResponse.model_validate(response.json())

                return data.creditsRemaining <= settings.BALANCE_REFILL_THRESHOLD
            except HTTPException:
                raise
            except Exception:
                logger.exception(f"Error while checking balance of subscription credit")
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    async def refill_subscription_balance(self):
        async with self.limiter:
            try:
                url = f"{self.base_url}/api/v1/aedbx/aerodatabox/subscriptions/balance/refill"
                response = await self.client.post(
                    url=url, json={"credits": settings.BALANCE_REFILL_AMMOUNT}
                )

                if response.status_code != 200:
                    logger.warning(
                        f"Aerodatabox responded with status code={response.status_code} while re-filling subscription balance"
                    )
                    raise HTTPException(status_code=response.status_code)

            except HTTPException:
                raise
            except Exception:
                logger.exception(
                    f"Error while re-filling balance of subscription credit"
                )
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


aerodatabox_fetcher_service = AerodataboxFetcherService()


@app.get("/flights")
async def fetch_single_flight(
    full_number: str = Query(...), departure_date: str = Query(...)
):
    return await aerodatabox_fetcher_service.fetch_single_flight(
        full_number=full_number, departure_date=departure_date
    )


@app.get("/airport-flights")
async def get_airport_flights(
    airport_iata: str = Query(...),
    departure_date: str = Query(...),
    time_window: Literal["morning", "afternoon"] = Query(...),
    direction: Literal["Departure", "Arrival", "Both"] = Query("Both"),
):
    return await aerodatabox_fetcher_service.fetch_airport_flights(
        airport_iata=airport_iata,
        departure_date=departure_date,
        time_window=time_window,
        direction=direction,
    )


@app.post("/create-webhook")
async def create_webhook(flight_full_number: str = Query(...)):
    is_credit_low = await aerodatabox_fetcher_service.is_subscription_balance_low()

    if is_credit_low:
        await aerodatabox_fetcher_service.refill_subscription_balance()

    return await aerodatabox_fetcher_service.create_webhook(
        flight_full_number=flight_full_number
    )


@app.delete("/delete-webhook")
async def delete_webhook(subscription_id: str = Query(...)):
    return await aerodatabox_fetcher_service.delete_webhook(
        subscription_id=subscription_id
    )
