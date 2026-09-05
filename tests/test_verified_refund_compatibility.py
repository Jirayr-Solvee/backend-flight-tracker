import base64
import json
import unittest
from unittest.mock import Mock, patch

from tests import test_subscription_lifecycle as fixtures  # isolated test settings
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.models.JWSTransactionDecodedPayload import JWSTransactionDecodedPayload
from appstoreserverlibrary.models.LibraryUtility import _get_cattrs_converter

from core.models.apple_ads import AppStoreRevenueEvent
from core.models.transaction import Transaction
from core.services.app_store.service import AppStoreService
from core.services.revenue_measurement import (
    refunded_milliunits, upsert_verified_revenue_event, upsert_verified_transaction,
)


class VerifiedRefundCompatibilityTests(unittest.TestCase):
    setUp = fixtures.SubscriptionLifecycleTests.setUp
    tearDown = fixtures.SubscriptionLifecycleTests.tearDown

    def test_real_pinned_model_preserves_additive_field_only_after_verification(self):
        for percentage in (50_000, 0, 100_000):
            with self.subTest(percentage=percentage):
                raw = {"transactionId": "fixture", "originalTransactionId": "original",
                       "productId": "yearly", "purchaseDate": 1_000, "signedDate": 2_000,
                       "price": 40_000, "currency": "USD", "environment": "Production",
                       "revocationDate": 3_000, "revocationPercentage": percentage}
                model = _get_cattrs_converter(JWSTransactionDecodedPayload).structure(raw, JWSTransactionDecodedPayload)
                encoded = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")
                signed = f"header.{encoded}.signature-fixture"
                verifier = Mock()
                verifier.verify_and_decode_signed_transaction.return_value = model
                with (patch.object(AppStoreService, "_get_root_certs", return_value=[]),
                      patch.object(AppStoreService, "_get_verifier", return_value=verifier),
                      patch("core.services.app_store.service.get_apple_environments", return_value=[Environment.PRODUCTION])):
                    result = AppStoreService.process_transaction(signed)
                verifier.verify_and_decode_signed_transaction.assert_called_once_with(signed_transaction=signed)
                self.assertEqual(result.revocationPercentage, percentage)
                result.transactionId = f"fixture-{percentage}"
                row = upsert_verified_revenue_event(session=self.session, decoded_jws=result, user_id="fixture-user")
                self.assertEqual(row.revocation_percentage, percentage)
                self.assertEqual(refunded_milliunits(40_000, row.revocation_percentage), 40_000 * percentage // 100_000)

    def test_invalid_signature_never_uses_compatibility_parser(self):
        verifier = Mock()
        verifier.verify_and_decode_signed_transaction.side_effect = ValueError("invalid test signature")
        with (patch.object(AppStoreService, "_get_root_certs", return_value=[]),
              patch.object(AppStoreService, "_get_verifier", return_value=verifier),
              patch("core.services.app_store.service.get_apple_environments", return_value=[Environment.PRODUCTION]),
              patch.object(AppStoreService, "_preserve_verified_revocation_percentage") as adapter):
            self.assertIsNone(AppStoreService.process_transaction("invalid-fixture"))
            adapter.assert_not_called()

    def test_unknown_signature_freshness_cannot_erase_known_refund(self):
        decoded = fixtures.SubscriptionLifecycleTests.transaction(signed_date=None, revoked_date=5_000)
        upsert_verified_transaction(session=self.session, decoded_jws=decoded)
        upsert_verified_revenue_event(session=self.session, decoded_jws=decoded, user_id="user-1")
        self.session.commit()
        decoded.revocationDate = None
        upsert_verified_transaction(session=self.session, decoded_jws=decoded)
        upsert_verified_revenue_event(session=self.session, decoded_jws=decoded, user_id="user-1")
        self.session.commit()
        self.assertEqual(self.session.get(Transaction, decoded.transactionId).revoked_date, 5_000)
        self.assertEqual(self.session.get(AppStoreRevenueEvent, decoded.transactionId).revoked_date_ms, 5_000)


if __name__ == "__main__":
    unittest.main()
