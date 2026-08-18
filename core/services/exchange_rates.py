import csv
import io
from typing import Iterable

import httpx


ECB_EXCHANGE_RATE_URL = "https://data-api.ecb.europa.eu/service/data/EXR"


class ExchangeRateError(RuntimeError):
    pass


async def fetch_latest_ecb_rates(
    currencies: Iterable[str],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, float]:
    """Return latest ECB reference rates expressed as currency units per EUR."""

    requested = sorted(
        {
            currency.upper()
            for currency in currencies
            if currency and currency.upper() not in {"EUR", "UNKNOWN"}
        }
    )
    rates = {"EUR": 1.0}
    if not requested:
        return rates

    external_client = client is not None
    http_client = client or httpx.AsyncClient(timeout=20.0)
    try:
        series = "+".join(requested)
        response = await http_client.get(
            f"{ECB_EXCHANGE_RATE_URL}/D.{series}.EUR.SP00.A",
            params={
                "lastNObservations": 1,
                "format": "csvdata",
                "detail": "dataonly",
            },
            headers={"Accept": "text/csv"},
        )
        if not 200 <= response.status_code < 300:
            raise ExchangeRateError(
                f"ECB exchange-rate request failed ({response.status_code})"
            )
        for row in csv.DictReader(io.StringIO(response.text)):
            currency = (
                row.get("CURRENCY")
                or row.get("currency")
                or row.get("Currency")
            )
            observation = (
                row.get("OBS_VALUE")
                or row.get("obs_value")
                or row.get("Obs Value")
            )
            if not currency or not observation:
                continue
            try:
                rates[currency.upper()] = float(observation)
            except ValueError:
                continue
        return rates
    finally:
        if not external_client:
            await http_client.aclose()
