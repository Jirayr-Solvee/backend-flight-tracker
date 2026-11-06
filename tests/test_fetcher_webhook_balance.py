import asyncio
import unittest
from unittest import mock

from aiolimiter import AsyncLimiter
from fastapi import HTTPException

from tests import test_experiment_reporting as _settings_fixture  # noqa: F401

from core.config import settings
from core.fetcher_service import AerodataboxFetcherService


class FetcherWebhookBalanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fetcher = object.__new__(AerodataboxFetcherService)
        self.fetcher.base_url = "https://provider.example"
        self.fetcher.client = mock.Mock()
        self.fetcher.client.post = mock.AsyncMock()
        self.fetcher.limiter = AsyncLimiter(1000, 1)
        self.fetcher.balance_lock = asyncio.Lock()
        self.fetcher.webhook_lock = asyncio.Lock()
        self.fetcher.balance = settings.BALANCE_REFILL_THRESHOLD
        self.fetcher.webhook_bad_request_until = {}
        self.fetcher.latest_webhook_flight_number = None
        self.fetcher.latest_webhook_id = None

    async def test_successful_refill_advances_balance_once_for_concurrent_calls(self):
        self.fetcher.client.post.return_value = mock.Mock(status_code=200)

        await asyncio.gather(*(
            self.fetcher.refill_subscription_balance() for _ in range(3)
        ))

        self.assertEqual(self.fetcher.client.post.await_count, 1)
        self.assertEqual(
            self.fetcher.balance,
            settings.BALANCE_REFILL_THRESHOLD + settings.BALANCE_REFILL_AMMOUNT,
        )

    async def test_failed_refill_does_not_advance_balance(self):
        self.fetcher.client.post.return_value = mock.Mock(status_code=429)

        with self.assertRaises(HTTPException):
            await self.fetcher.refill_subscription_balance()

        self.assertEqual(self.fetcher.balance, settings.BALANCE_REFILL_THRESHOLD)

    async def test_successive_webhooks_share_one_refill(self):
        self.fetcher.client.post.side_effect = [
            mock.Mock(status_code=200),
            mock.Mock(status_code=200, json=lambda: {"id": "first"}),
            mock.Mock(status_code=200, json=lambda: {"id": "second"}),
        ]

        await self.fetcher.create_webhook("AA100")
        await self.fetcher.create_webhook("AA200")

        self.assertEqual(self.fetcher.client.post.await_count, 3)
        self.assertEqual(
            self.fetcher.balance,
            settings.BALANCE_REFILL_THRESHOLD + settings.BALANCE_REFILL_AMMOUNT,
        )

    async def test_bad_webhook_request_is_not_resent_or_refilled_immediately(self):
        self.fetcher.balance = settings.BALANCE_REFILL_THRESHOLD + 1
        self.fetcher.client.post.return_value = mock.Mock(status_code=400)

        for _ in range(2):
            with self.assertRaises(HTTPException) as raised:
                await self.fetcher.create_webhook("AA100")
            self.assertEqual(raised.exception.status_code, 400)

        self.assertEqual(self.fetcher.client.post.await_count, 1)
        self.assertEqual(self.fetcher.balance, settings.BALANCE_REFILL_THRESHOLD + 1)
