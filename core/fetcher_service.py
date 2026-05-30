import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Literal

import httpx
from aiolimiter import AsyncLimiter
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel

from .background_tasks import (check_and_create_webhook_for_flight,
                               remove_hanging_webhooks)
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)

logger = logging.getLogger()


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

        self.balance: int = 0
        self.latest_webhook_flight_number: str | None = None
        self.latest_webhook_id: str | None = None
        self.cache: dict[str, tuple[float, Any]] = {}

    async def _cached_json(
        self,
        cache_key: str,
        ttl_seconds: int,
        fetcher: Callable[[], Awaitable[Any]],
    ) -> Any:
        now = time.time()
        cached = self.cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        data = await fetcher()
        self.cache[cache_key] = (now + ttl_seconds, data)
        return data

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

    async def fetch_flight_delays(self, full_number: str) -> Any:
        """
        Get historical delay statistics for a flight number.
        """
        full_number = full_number.strip().replace(" ", "").upper()
        cache_key = f"flight-delays:{full_number}"

        async def fetch():
            async with self.limiter:
                url = f"{self.base_url}/api/v1/aedbx/aerodatabox/flights/{full_number}/delays"
                response = await self.client.get(url)

                if response.status_code == 204:
                    return {}

                if response.status_code != 200:
                    logger.warning(
                        f"Aerodatabox responded with status={response.status_code} for flight delay stats, full_number={full_number}"
                    )
                    raise HTTPException(status_code=response.status_code)

                return response.json()

        return await self._cached_json(cache_key, ttl_seconds=60 * 60 * 12, fetcher=fetch)

    async def fetch_airport_delay(
        self, airport_iata: str, date_local: str | None = None
    ) -> Any:
        """
        Get current or historical delay statistics for one airport.
        """
        airport_iata = airport_iata.strip().upper()
        cache_key = f"airport-delay:{airport_iata}:{date_local or 'current'}"

        async def fetch():
            async with self.limiter:
                url = (
                    f"{self.base_url}/api/v1/aedbx/aerodatabox"
                    f"/airports/Iata/{airport_iata}/delays"
                )
                if date_local:
                    url = f"{url}/{date_local}"

                response = await self.client.get(url)

                if response.status_code == 204:
                    return {}

                if response.status_code != 200:
                    logger.warning(
                        f"Aerodatabox responded with status={response.status_code} for airport delay stats, airport_iata={airport_iata}, date_local={date_local}"
                    )
                    raise HTTPException(status_code=response.status_code)

                return response.json()

        ttl = 60 * 15 if date_local is None else 60 * 60 * 24
        return await self._cached_json(cache_key, ttl_seconds=ttl, fetcher=fetch)

    async def fetch_route_daily_statistics(
        self, airport_iata: str, date_local: str
    ) -> Any:
        """
        Get daily route statistics for an airport.
        """
        airport_iata = airport_iata.strip().upper()
        cache_key = f"route-daily-statistics:{airport_iata}:{date_local}"

        async def fetch():
            async with self.limiter:
                url = (
                    f"{self.base_url}/api/v1/aedbx/aerodatabox"
                    f"/airports/Iata/{airport_iata}/stats/routes/daily/{date_local}"
                )
                response = await self.client.get(url)

                if response.status_code == 204:
                    return {}

                if response.status_code != 200:
                    logger.warning(
                        f"Aerodatabox responded with status={response.status_code} for route daily statistics, airport_iata={airport_iata}, date_local={date_local}"
                    )
                    raise HTTPException(status_code=response.status_code)

                return response.json()

        return await self._cached_json(cache_key, ttl_seconds=60 * 60 * 24, fetcher=fetch)

    async def create_webhook(self, flight_full_number: str) -> Any:
        url = f"{self.base_url}/api/v1/aedbx/aerodatabox/subscriptions/webhook/FlightByNumber/{flight_full_number}?useCredits=true"
        payload = {
            "url": f"{settings.API_URL}/webhook/aerodatabox",
            "maxDeliveryRetries": 1,
        }

        async with self.limiter:
            try:
                if flight_full_number == self.latest_webhook_flight_number:
                    return {
                        "id": self.latest_webhook_id,
                        "isActive": True,
                        "createdOnUtc": "maybe a moment ago ;)",
                    }

                response = await self.client.post(url=url, json=payload)
                if response.status_code != 200:
                    logger.warning(
                        f"Aerodatabox responded with status code={response.status_code} while creating a webhook sub for flight_full_number={flight_full_number}"
                    )
                    raise HTTPException(status_code=response.status_code)

                respnose_data = response.json()
                if respnose_data:
                    webhook_id = respnose_data.get("id")
                    if webhook_id:
                        self.latest_webhook_id = webhook_id
                        self.latest_webhook_flight_number = flight_full_number

                return respnose_data

            except HTTPException:
                raise
            except Exception:
                logger.exception(
                    f"Error during creation of webhook subscription for flight_full_number={flight_full_number}"
                )
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    async def delete_webhook(self, subscription_id: str) -> dict:
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

                if self.latest_webhook_id == subscription_id:
                    self.latest_webhook_id = None
                    self.latest_webhook_flight_number = None

                return {"detail": "deleted"}
            except HTTPException:
                raise
            except Exception:
                logger.exception(
                    f"Error during deletion of webhook subscription_id={subscription_id}"
                )
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    async def is_subscription_balance_low(self) -> bool:
        return self.balance <= settings.BALANCE_REFILL_THRESHOLD

    async def get_balance(self) -> int:
        async with self.limiter:
            try:
                url = f"{self.base_url}/api/v1/aedbx/aerodatabox/subscriptions/balance"
                response = await self.client.get(url)

                if response.status_code == 400:
                    logger.warning(
                        f"Aerodatabox responded with status code={response.status_code} while checking subscription balance, empty balance"
                    )
                    return 0
                elif response.status_code != 200:
                    logger.warning(
                        f"Aerodatabox responded with status code={response.status_code} while checking subscription balance"
                    )
                    return 0

                data = BalanceResponse.model_validate(response.json())
                return data.creditsRemaining
            except HTTPException:
                raise
            except:
                logger.exception(f"Error while retriving balance")
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    aerodatabox_fetcher_service.balance = (
        await aerodatabox_fetcher_service.get_balance()
    )
    logger.info(f"aerodatabox balance: {aerodatabox_fetcher_service.balance}")

    background_fn = [remove_hanging_webhooks(), check_and_create_webhook_for_flight()]
    tasks = [asyncio.create_task(fn) for fn in background_fn]

    try:
        yield
    finally:
        for t in tasks:
            t.cancel()

        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass


app = FastAPI(lifespan=lifespan)


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


@app.get("/flight-delays")
async def get_flight_delays(full_number: str = Query(...)):
    return await aerodatabox_fetcher_service.fetch_flight_delays(
        full_number=full_number
    )


@app.get("/airport-delay")
async def get_airport_delay(
    airport_iata: str = Query(...),
    date_local: str | None = Query(None),
):
    return await aerodatabox_fetcher_service.fetch_airport_delay(
        airport_iata=airport_iata, date_local=date_local
    )


@app.get("/route-daily-statistics")
async def get_route_daily_statistics(
    airport_iata: str = Query(...),
    date_local: str = Query(...),
):
    return await aerodatabox_fetcher_service.fetch_route_daily_statistics(
        airport_iata=airport_iata, date_local=date_local
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


@app.put("/confirm-webhook-notification")
async def confirm_webhook_notification():
    aerodatabox_fetcher_service.balance = aerodatabox_fetcher_service.balance - 1
