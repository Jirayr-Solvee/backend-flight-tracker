"""Synthetic signed-payload failures must not leak through verifier diagnostics."""

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Existing inert test configuration is installed before importing application code.
from tests import test_experiment_reporting as _test_environment

from appstoreserverlibrary.models.Environment import Environment
from core.services.app_store import service as app_store


SIGNED_PAYLOAD = "synthetic.header.private_signed_payload.signature"
TOKEN = "SYNTHETIC_APPLE_AUTHORIZATION_CREDENTIAL"
PRIVATE_INFO = "synthetic-private-account@example.invalid"
SENTINELS = (SIGNED_PAYLOAD, TOKEN, PRIVATE_INFO)
ENVIRONMENTS = [Environment.XCODE, Environment.PRODUCTION, Environment.SANDBOX]
PROCESSORS = (
    ("transaction", "process_transaction", "verify_and_decode_signed_transaction", "signed_transaction"),
    ("notification", "process_notification", "verify_and_decode_notification", "signed_payload"),
    ("renewal_info", "process_renewal_info", "verify_and_decode_renewal_info", "signed_renewal_info"),
)


class SyntheticVerifierError(RuntimeError):
    def __init__(self):
        self.userInfo = {"signedPayload": SIGNED_PAYLOAD, "token": TOKEN, "account": PRIVATE_INFO}
        super().__init__(f"synthetic SDK failure: {self.userInfo}")


def fail_with_sensitive_chain(*args, **kwargs):
    try:
        raise RuntimeError(TOKEN)
    except RuntimeError as cause:
        raise SyntheticVerifierError() from cause


class AppStoreCredentialLoggingTests(unittest.TestCase):
    def assert_only_bounded_failure(self, captured, operation):
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.msg,
                         f"app_store_verification_failed operation={operation} attempted_environments=%s")
        self.assertEqual(record.args, ([environment.value for environment in ENVIRONMENTS],))
        self.assertFalse(record.exc_info)
        self.assertFalse(record.stack_info)
        rendered = logging.Formatter("%(levelname)s %(message)s").format(record)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, rendered)
            self.assertNotIn(sentinel, repr(record.__dict__))
        self.assertNotIn("Traceback", rendered)
        self.assertNotIn("userInfo", rendered)

    def test_all_three_verifier_exhaustions_hide_jws_error_user_info_and_chain(self):
        for operation, processor_name, verifier_name, argument_name in PROCESSORS:
            with self.subTest(operation=operation):
                verifier = MagicMock()
                getattr(verifier, verifier_name).side_effect = fail_with_sensitive_chain
                with patch.object(app_store.AppStoreService, "_get_root_certs", return_value=[b"fixture-root"]), \
                        patch.object(app_store.AppStoreService, "_get_verifier", return_value=verifier) as factory, \
                        patch.object(app_store, "get_apple_environments", return_value=ENVIRONMENTS), \
                        patch.object(app_store.AppStoreService, "_preserve_verified_revocation_percentage") as adapter, \
                        self.assertLogs(app_store.logger, level="ERROR") as captured:
                    result = getattr(app_store.AppStoreService, processor_name)(SIGNED_PAYLOAD)
                self.assertIsNone(result)
                self.assert_only_bounded_failure(captured, operation)
                self.assertEqual([call.kwargs["environment"] for call in factory.call_args_list], ENVIRONMENTS)
                self.assertEqual(getattr(verifier, verifier_name).call_count, len(ENVIRONMENTS))
                for call in getattr(verifier, verifier_name).call_args_list:
                    self.assertEqual(call.kwargs, {argument_name: SIGNED_PAYLOAD})
                adapter.assert_not_called()

    def test_constructor_failure_also_exhausts_without_retaining_sensitive_errors(self):
        for operation, processor_name, _, _ in PROCESSORS:
            with self.subTest(operation=operation):
                with patch.object(app_store.AppStoreService, "_get_root_certs", return_value=[]), \
                        patch.object(app_store.AppStoreService, "_get_verifier",
                                     side_effect=fail_with_sensitive_chain) as factory, \
                        patch.object(app_store, "get_apple_environments", return_value=ENVIRONMENTS), \
                        self.assertLogs(app_store.logger, level="ERROR") as captured:
                    result = getattr(app_store.AppStoreService, processor_name)(SIGNED_PAYLOAD)
                self.assertIsNone(result)
                self.assertEqual(factory.call_count, len(ENVIRONMENTS))
                self.assert_only_bounded_failure(captured, operation)

    def test_primary_and_each_fallback_success_preserve_verified_result_and_order(self):
        for operation, processor_name, verifier_name, argument_name in PROCESSORS:
            for success_index in range(len(ENVIRONMENTS)):
                with self.subTest(operation=operation, success_index=success_index):
                    # Existing verified financial metadata must remain untouched.
                    verified = SimpleNamespace(transactionId="fixture-transaction", revocationPercentage=50_000)
                    verifiers = [MagicMock() for _ in ENVIRONMENTS]
                    for index, verifier in enumerate(verifiers):
                        if index < success_index:
                            getattr(verifier, verifier_name).side_effect = fail_with_sensitive_chain
                        else:
                            getattr(verifier, verifier_name).return_value = verified
                    original_adapter = app_store.AppStoreService._preserve_verified_revocation_percentage
                    with patch.object(app_store.AppStoreService, "_get_root_certs", return_value=[]), \
                            patch.object(app_store.AppStoreService, "_get_verifier", side_effect=verifiers) as factory, \
                            patch.object(app_store, "get_apple_environments", return_value=ENVIRONMENTS), \
                            patch.object(app_store.AppStoreService, "_preserve_verified_revocation_percentage",
                                         wraps=original_adapter) as adapter, \
                            patch.object(app_store.logger, "error") as error_log, \
                            patch.object(app_store.logger, "info") as info_log:
                        result = getattr(app_store.AppStoreService, processor_name)(SIGNED_PAYLOAD)
                    self.assertIs(result, verified)
                    self.assertEqual(result.revocationPercentage, 50_000)
                    self.assertEqual([call.kwargs["environment"] for call in factory.call_args_list],
                                     ENVIRONMENTS[:success_index + 1])
                    for verifier in verifiers[:success_index + 1]:
                        getattr(verifier, verifier_name).assert_called_once_with(**{argument_name: SIGNED_PAYLOAD})
                    for verifier in verifiers[success_index + 1:]:
                        getattr(verifier, verifier_name).assert_not_called()
                    if operation == "transaction":
                        adapter.assert_called_once_with(verified, SIGNED_PAYLOAD)
                    else:
                        adapter.assert_not_called()
                    error_log.assert_not_called()
                    if success_index:
                        self.assertEqual(info_log.call_count, 1)
                        self.assertEqual(info_log.call_args.args[1], ENVIRONMENTS[success_index].value)
                        for sentinel in SENTINELS:
                            self.assertNotIn(sentinel, repr(info_log.call_args))
                    else:
                        info_log.assert_not_called()
