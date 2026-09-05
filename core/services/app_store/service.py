import base64
import json
import logging

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

from ...config import JWSEnvironment, settings

logger = logging.getLogger(__name__)


def get_apple_environment() -> Environment:
    mapping = {
        JWSEnvironment.XCODE: Environment.XCODE,
        JWSEnvironment.SANDBOX: Environment.SANDBOX,
        JWSEnvironment.PRODUCTION: Environment.PRODUCTION,
    }

    return mapping[settings.JWS_ENV]


def get_apple_environments() -> list[Environment]:
    primary = get_apple_environment()
    environments = [primary]

    for fallback in (Environment.PRODUCTION, Environment.SANDBOX):
        if fallback not in environments:
            environments.append(fallback)

    return environments


class AppStoreService:
    @staticmethod
    def _preserve_verified_revocation_percentage(payload, signed_jws: str):
        """Bridge an additive Apple field omitted by the pinned SDK's model.

        Call only after the SDK verifies this exact JWS's signature, bundle and
        environment. Never use this parser as verification or retain raw JWS.
        """
        if getattr(payload, "revocationPercentage", None) is not None:
            return payload
        try:
            encoded = signed_jws.split(".")[1]
            raw = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            value = raw.get("revocationPercentage")
            if (str(raw.get("transactionId")) == str(payload.transactionId)
                    and type(value) is int and 0 <= value <= 100_000):
                payload.revocationPercentage = value
        except (ValueError, IndexError, AttributeError, TypeError):
            # Missing optional financial metadata never reverses a successful
            # verification. No payload/exception text may enter application logs.
            logger.warning("Verified transaction optional refund metadata unavailable")
        return payload

    @staticmethod
    def _get_root_certs() -> list[bytes]:
        with open(settings.APPLE_ROOT_CERT_PATH, "rb") as f:
            return [f.read()]

    @staticmethod
    def _get_verifier(
        root_certs: list[bytes],
        environment: Environment,
    ) -> SignedDataVerifier:
        return SignedDataVerifier(
            root_certificates=root_certs,
            bundle_id=settings.BUNDLE_ID,
            app_apple_id=settings.APP_APPLE_ID,
            environment=environment,
            enable_online_checks=True,
        )

    @staticmethod
    def _environment_name(environment: Environment) -> str:
        return getattr(environment, "value", str(environment))

    @staticmethod
    def process_transaction(signed_jws: str):
        root_certs = AppStoreService._get_root_certs()
        environments = get_apple_environments()
        errors: list[Exception] = []

        for environment in environments:
            try:
                verifier = AppStoreService._get_verifier(
                    root_certs=root_certs,
                    environment=environment,
                )
                payload = verifier.verify_and_decode_signed_transaction(
                    signed_transaction=signed_jws
                )
                payload = AppStoreService._preserve_verified_revocation_percentage(payload, signed_jws)

                if environment != environments[0]:
                    logger.info(
                        "Accepted App Store transaction using fallback environment=%s",
                        AppStoreService._environment_name(environment),
                    )

                return payload
            except Exception as exc:
                errors.append(exc)

        attempted = [
            AppStoreService._environment_name(environment)
            for environment in environments
        ]
        logger.error(
            "process_transaction failed to decode signed_jws after attempted_environments=%s",
            attempted,
            exc_info=(
                (type(errors[-1]), errors[-1], errors[-1].__traceback__)
                if errors
                else None
            ),
        )
        return None

    @staticmethod
    def process_notification(signed_payload: str):
        root_certs = AppStoreService._get_root_certs()
        environments = get_apple_environments()
        errors: list[Exception] = []

        for environment in environments:
            try:
                verifier = AppStoreService._get_verifier(
                    root_certs=root_certs,
                    environment=environment,
                )
                result = verifier.verify_and_decode_notification(
                    signed_payload=signed_payload
                )

                if environment != environments[0]:
                    logger.info(
                        "Accepted App Store notification using fallback environment=%s",
                        AppStoreService._environment_name(environment),
                    )

                return result
            except Exception as exc:
                errors.append(exc)

        attempted = [
            AppStoreService._environment_name(environment)
            for environment in environments
        ]
        logger.error(
            "process_notification failed to decode signed payload after attempted_environments=%s",
            attempted,
            exc_info=(
                (type(errors[-1]), errors[-1], errors[-1].__traceback__)
                if errors
                else None
            ),
        )
        return None

    @staticmethod
    def process_renewal_info(signed_renewal_info: str):
        root_certs = AppStoreService._get_root_certs()
        environments = get_apple_environments()
        errors: list[Exception] = []

        for environment in environments:
            try:
                verifier = AppStoreService._get_verifier(
                    root_certs=root_certs,
                    environment=environment,
                )
                payload = verifier.verify_and_decode_renewal_info(
                    signed_renewal_info=signed_renewal_info
                )

                if environment != environments[0]:
                    logger.info(
                        "Accepted App Store renewal info using fallback environment=%s",
                        AppStoreService._environment_name(environment),
                    )

                return payload
            except Exception as exc:
                errors.append(exc)

        attempted = [
            AppStoreService._environment_name(environment)
            for environment in environments
        ]
        logger.error(
            "process_renewal_info failed to decode signed payload after attempted_environments=%s",
            attempted,
            exc_info=(
                (type(errors[-1]), errors[-1], errors[-1].__traceback__)
                if errors
                else None
            ),
        )
        return None
