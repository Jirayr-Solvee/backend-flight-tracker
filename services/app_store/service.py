import logging

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

from ...config import settings

logger = logging.getLogger(__name__)

from ...config import JWSEnvironment, settings


def get_apple_environment() -> Environment:
    mapping = {
        JWSEnvironment.XCODE: Environment.XCODE,
        JWSEnvironment.SANDBOX: Environment.SANDBOX,
        JWSEnvironment.PRODUCTION: Environment.PRODUCTION,
    }

    return mapping[settings.JWS_ENV]


class AppStoreService:

    @staticmethod
    def process_transaction(signed_jws: str):
        try:
            with open(settings.APPLE_ROOT_CERT_PATH, "rb") as f:
                root_certs = [f.read()]

            verifier = SignedDataVerifier(
                root_certificates=root_certs,
                bundle_id=settings.BUNDLE_ID,
                app_apple_id=settings.APP_APPLE_ID,
                environment=get_apple_environment(),
                enable_online_checks=True,
            )
            payload = verifier.verify_and_decode_signed_transaction(
                signed_transaction=signed_jws
            )

            return payload
        except Exception:
            logger.exception(
                f"process_transaction failed to decode following signed_jws={signed_jws}"
            )
            return None

    @staticmethod
    def process_notification(signed_payload: str):
        try:
            with open(settings.APPLE_ROOT_CERT_PATH, "rb") as f:
                root_certs = [f.read()]

            verifier = SignedDataVerifier(
                root_certificates=root_certs,
                bundle_id=settings.BUNDLE_ID,
                app_apple_id=settings.APP_APPLE_ID,
                environment=get_apple_environment(),
                enable_online_checks=True,
            )
            result = verifier.verify_and_decode_notification(
                signed_payload=signed_payload
            )

            return result
        except Exception:
            logger.exception(
                f"process_notification failed to decode following notification signed payload={signed_payload}"
            )
            return None
