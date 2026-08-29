import os
import unittest
from pathlib import Path
from unittest.mock import patch


TEST_FILE = Path(__file__).resolve()
REPOSITORY_ROOT = TEST_FILE.parents[1]

for key, value in {
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_BUCKET_NAME": "test",
    "AWS_REGION": "eu-north-1",
    "LAMBDA_FUNCTION_AUTH_TOKEN": "test",
    "GEMINI_API_KEY": "test",
    "API_URL": "https://example.invalid",
    "JWT_SECRET": "test",
    "JWT_ALGORITHM": "HS256",
    "JWT_EXPIRE_DAYS": "1",
    "KEY_ID": "test",
    "ISSUER_ID": "test",
    "BUNDLE_ID": "com.zhirayr.Flight-tracker-shared",
    "APP_APPLE_ID": "1",
    "TEAM_ID": "test",
    "X_API_MARKET_KEY": "test",
    "AERODATABOX_SERVICE_URL": "https://example.invalid",
    "BALANCE_REFILL_AMMOUNT": "1",
    "BALANCE_REFILL_THRESHOLD": "1",
    "JWS_ENV": "XCODE",
    "MAX_PREMIUM_HOURS": "1",
    "APPLE_ISSUER": "test",
    "APPLE_KEYS_URL": "https://example.invalid",
    "GUEST_KEY": "test",
    "APN_KEY_PATH": str(TEST_FILE),
    "APPLE_ROOT_CERT_PATH": str(TEST_FILE),
    "AIRLINE_MAP_JSON": str(REPOSITORY_ROOT / "iata_to_icao.json"),
}.items():
    os.environ.setdefault(key, value)


from core.models.aerodatabox import (
    AerodataboxOriginAndDestinationInformationWebhook,
)
from core.models.flight import Departure
from core.models.notification import DeviceInfo, Notification, NotificationBatch
from core.routers.webhook import partition_notification_refresh_tokens
from core.services.apn.service import ApnService
from core.services.apn.utils import (
    consolidate_notification_batches,
    extract_nested_notifications_for_flight,
)


def device(*, localized: bool = False, token: str = "token-1") -> DeviceInfo:
    return DeviceInfo(
        token=token,
        badge=1,
        user_id="user-1",
        notification_count=0,
        supports_localized_push=localized,
    )


class NotificationExtractionTests(unittest.TestCase):
    def test_provider_snapshot_is_consolidated_to_highest_priority_alert(self):
        devices = [device()]
        low = NotificationBatch(
            notification=Notification(
                title="Aircraft updated",
                body="Aircraft changed",
                flight_id=42,
                update_type="aircraft",
                priority=50,
            ),
            devices=devices,
            invoke_review=True,
        )
        high = NotificationBatch(
            notification=Notification(
                title="Gate updated",
                body="Gate B12",
                flight_id=42,
                update_type="gate",
                priority=110,
            ),
            devices=devices,
        )

        result = consolidate_notification_batches([low, high])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].notification.update_type, "gate")
        self.assertTrue(result[0].invoke_review)

    def test_retracted_provider_detail_does_not_create_none_alert(self):
        db_departure = Departure(gate="B12")
        webhook_departure = (
            AerodataboxOriginAndDestinationInformationWebhook.model_construct(
                terminal=None,
                checkInDesk=None,
                gate=None,
                baggageBelt=None,
                scheduledTime=None,
                predictedTime=None,
                revisedTime=None,
                runwayTime=None,
            )
        )

        result = extract_nested_notifications_for_flight(
            flight_id=42,
            flight_number="AA100",
            db_info=db_departure,
            webhook_data=webhook_departure,
            devices_info=[device()],
        )

        self.assertEqual(result, [])

    def test_review_eligible_token_wins_over_plain_refresh(self):
        normal_batch = NotificationBatch(
            notification=Notification(
                title="Updated",
                body="Updated",
                flight_id=42,
                update_type="time",
            ),
            devices=[device(token="shared"), device(token="normal")],
        )
        review_batch = NotificationBatch(
            notification=Notification(
                title="Useful update",
                body="Gate B12",
                flight_id=42,
                update_type="gate",
            ),
            devices=[device(token="shared"), device(token="review")],
            invoke_review=True,
        )

        refresh_tokens, review_tokens = partition_notification_refresh_tokens(
            [normal_batch, review_batch]
        )

        self.assertEqual(refresh_tokens, {"normal"})
        self.assertEqual(review_tokens, {"shared", "review"})


class NotificationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_app_receives_localization_keys_and_review_flag(self):
        captured = []

        class FakeAPNsClient:
            async def send_notification(self, request):
                captured.append(request)

        notification = Notification(
            title="Flight AA100 details updated",
            body="Gate B12 is now available for flight AA100.",
            flight_id=42,
            update_type="gate",
            new_value="B12",
            title_loc_key="Flight %@ details updated",
            title_loc_args=["AA100"],
            body_loc_key="Gate %@ is now available for flight %@.",
            body_loc_args=["B12", "AA100"],
        )
        batch = NotificationBatch(
            notification=notification,
            devices=[device(localized=True)],
            invoke_review=True,
        )

        with patch(
            "core.services.apn.service.get_apns_client",
            return_value=FakeAPNsClient(),
        ):
            await ApnService.send_multiple_push_notification(batch)

        self.assertEqual(len(captured), 1)
        message = captured[0].message
        self.assertEqual(
            message["aps"]["alert"],
            {
                "title-loc-key": "Flight %@ details updated",
                "title-loc-args": ["AA100"],
                "loc-key": "Gate %@ is now available for flight %@.",
                "loc-args": ["B12", "AA100"],
            },
        )
        self.assertTrue(message["request_review"])

    async def test_released_app_keeps_english_fallback(self):
        captured = []

        class FakeAPNsClient:
            async def send_notification(self, request):
                captured.append(request)

        notification = Notification(
            title="Flight AA100 details updated",
            body="Gate B12 is now available for flight AA100.",
            flight_id=42,
            update_type="gate",
            title_loc_key="Flight %@ details updated",
            body_loc_key="Gate %@ is now available for flight %@.",
        )
        batch = NotificationBatch(
            notification=notification,
            devices=[device(localized=False)],
        )

        with patch(
            "core.services.apn.service.get_apns_client",
            return_value=FakeAPNsClient(),
        ):
            await ApnService.send_multiple_push_notification(batch)

        self.assertEqual(
            captured[0].message["aps"]["alert"],
            {
                "title": "Flight AA100 details updated",
                "body": "Gate B12 is now available for flight AA100.",
            },
        )


if __name__ == "__main__":
    unittest.main()
