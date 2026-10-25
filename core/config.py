from enum import Enum
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .closable_flow_policy import settings_error as closable_flow_settings_error


class JWSEnvironment(str, Enum):
    XCODE = "XCODE"
    SANDBOX = "SANDBOX"
    PRODUCTION = "PRODUCTION"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", hide_input_in_errors=True)

    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_BUCKET_NAME: str
    AWS_REGION: str

    LAMBDA_FUNCTION_AUTH_TOKEN: str

    # New protected SES prefix only. Empty bucket fails closed until the exact
    # reviewed receipt-rule/bucket-policy deployment config is installed.
    FORWARDED_EMAIL_BUCKET: str = ""
    FORWARDED_EMAIL_KEY_PREFIX: str = "authenticated-v1/"
    FORWARDED_EMAIL_RECIPIENT: str = "track@sofly.to"

    GEMINI_API_KEY: str

    API_URL: str

    JWT_SECRET: str
    JWT_ALGORITHM: str
    JWT_EXPIRE_DAYS: int

    KEY_ID: str
    ISSUER_ID: str
    BUNDLE_ID: str
    APP_APPLE_ID: int
    TEAM_ID: str

    X_API_MARKET_KEY: str

    AERODATABOX_SERVICE_URL: str
    ADSBEXCHANGE_API_KEY: str | None = None
    ADSBEXCHANGE_BASE_URL: str = "https://gateway.adsbexchange.com/api/aircraft/v2"
    ADSBEXCHANGE_AUTH_HEADER: str = "api-auth"
    ADSBEXCHANGE_GLOBAL_PATHS: str = "all"
    ADSBEXCHANGE_RAPIDAPI_HOST: str | None = None
    ADSBEXCHANGE_CACHE_TTL_SECONDS: int = 300
    GLOBAL_FLIGHT_POSITIONS_RESPONSE_LIMIT: int = 70
    GLOBAL_FLIGHT_POSITIONS_PER_CONTINENT_LIMIT: int = 10
    GLOBAL_FLIGHT_MAJOR_AIRLINES_ONLY: bool = True
    GLOBAL_FLIGHT_NORMALIZE_CALLSIGN_TO_IATA: bool = True
    GLOBAL_FLIGHT_RESOLVE_CANDIDATE_LIMIT: int = 4
    GLOBAL_FLIGHT_RESOLVE_CANDIDATE_TIMEOUT_SECONDS: float = 12.0

    BALANCE_REFILL_AMMOUNT: int
    BALANCE_REFILL_THRESHOLD: int

    DEV_ENV: bool = False
    JWS_ENV: JWSEnvironment

    MAX_PREMIUM_HOURS: int
    # A verified Lifetime purchase has no Apple expiry. Its server lease must not
    # depend on app launches; REFUND/REVOKE notifications clear it immediately.
    LIFETIME_PREMIUM_LEASE_DAYS: int = Field(default=365, ge=1, le=3650)

    APPLE_ISSUER: str
    APPLE_KEYS_URL: str

    GUEST_KEY: str

    APN_KEY_PATH: str
    APPLE_ROOT_CERT_PATH: str

    AIRLINE_MAP_JSON: str

    # Apple Ads measurement. The OAuth fields are optional so attribution and
    # App Store revenue collection can ship before API-read credentials exist.
    APPLE_ADS_CLIENT_ID: str | None = None
    APPLE_ADS_TEAM_ID: str | None = None
    APPLE_ADS_KEY_ID: str | None = None
    APPLE_ADS_PRIVATE_KEY_PATH: str | None = None
    APPLE_ADS_ORG_ID: int | None = None
    APPLE_ADS_API_BASE_URL: str = "https://api.searchads.apple.com/api/v5"
    APPLE_ADS_ATTRIBUTION_URL: str = "https://api-adservices.apple.com/api/v1/"

    # Remote assignment for the flight-detail paywall experiment. Changing the
    # mode or rollout only requires a backend restart; no App Store release.
    FLIGHT_DETAIL_PAYWALL_EXPERIMENT_MODE: Literal[
        "split", "control", "treatment", "off"
    ] = "split"
    FLIGHT_DETAIL_PAYWALL_TREATMENT_PERCENT: int = 50
    FLIGHT_DETAIL_PAYWALL_CONFIG_VERSION: str = "1"
    FLIGHT_DETAIL_PAYWALL_CACHE_TTL_SECONDS: int = 60

    # A new full-journey protocol, never an implicit extension of the old test.
    # Deployment/configuration changes require separate launch authorization.
    ACTIVATION_JOURNEY_PRODUCTION_ENROLLMENT_ENABLED: bool = False
    ACTIVATION_JOURNEY_NONPRODUCTION_ENROLLMENT_ENABLED: bool = False
    # Retained setting name: the share assigned to search_first_standard, not
    # every variant that omits goals. The remaining share uses the explicit arm.
    ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT: int = 50
    ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT: Literal[
        "goals_flight_detail", "search_first_flight_detail"
    ] = "goals_flight_detail"
    ACTIVATION_JOURNEY_CONFIG_VERSION: str = "draft_1"
    ACTIVATION_JOURNEY_FORCE_STANDARD_PAYWALL: bool = False
    ACTIVATION_JOURNEY_OPERATIONAL_CONFIG_VERSION: str = "draft_1"

    # Closable own-flight flow test (closable_flow_2026_10). Every switch defaults
    # off and needs a restart; bump the operational version on any change.
    # Revisions come only from the shared registry and are never reused: turning
    # Lifetime on means LIFETIME_ENABLED plus the next Lifetime revision together.
    CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED: bool = False
    CLOSABLE_FLOW_NONPRODUCTION_ENROLLMENT_ENABLED: bool = False
    # Test-arm share: closable_own_flight in revisions 1–4, matched_soft_paywall
    # in 5–6. Matched revisions require 50 in both environments. Never change
    # allocation during a revision. Nonproduction covers TestFlight/App Review.
    CLOSABLE_FLOW_TEST_PERCENT: int = Field(default=50, ge=0, le=100)
    CLOSABLE_FLOW_NONPRODUCTION_TEST_PERCENT: int = Field(default=50, ge=0, le=100)
    CLOSABLE_FLOW_MEASUREMENT_REVISION: int = 1
    # Explicit rollout selects 5 (annual/weekly) or 6 (+Lifetime) only after
    # the matching app is verified. Keeping defaults off avoids silent rollout.
    # Kill switch: no new enrollment, and future test-arm presentations use the
    # control experience. Assigned arms never change.
    CLOSABLE_FLOW_FORCE_CONTROL_DELIVERY: bool = False
    # Lifetime delivery in both arms (owner decision 2026-10-21). Emergency off
    # under a Lifetime revision requires stopping enrollment (kill switch or both
    # enrollment flags off); resume only with the next registry revision.
    CLOSABLE_FLOW_LIFETIME_ENABLED: bool = False
    CLOSABLE_FLOW_OPERATIONAL_CONFIG_VERSION: str = Field(
        default="1", min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$",
    )

    # Onboarding delay preview (closable_flow_2026_10 test arm): one real
    # prediction per account and installation. Off until the deploy is
    # verified. Caps bound uncached builds (each up to 3 provider calls);
    # cache hits are free. Changing a value requires a restart.
    DELAY_PREVIEW_ENABLED: bool = False
    DELAY_PREVIEW_DAILY_BUILD_CAP: int = Field(default=150, ge=0, le=5000)
    DELAY_PREVIEW_MINUTE_BUILD_CAP: int = Field(default=6, ge=0, le=60)

    # Public flight-share links. nginx maps /f/<token> to /public/share/<token>.
    # The page shell loads versioned website assets; bump with each asset release.
    FLIGHT_SHARE_BASE_URL: str = "https://sofly.to/f/"
    FLIGHT_SHARE_ASSET_VERSION: str = "1"

    @model_validator(mode="after")
    def closable_flow_registry(self):
        # Fail at startup (and so at the readiness check) rather than enrolling
        # anyone into an unknown or Lifetime revision without its switch.
        error = closable_flow_settings_error(
            revision=self.CLOSABLE_FLOW_MEASUREMENT_REVISION,
            lifetime_enabled=self.CLOSABLE_FLOW_LIFETIME_ENABLED,
            test_percent=self.CLOSABLE_FLOW_TEST_PERCENT,
            nonproduction_test_percent=self.CLOSABLE_FLOW_NONPRODUCTION_TEST_PERCENT,
            enrollment_enabled=(self.CLOSABLE_FLOW_PRODUCTION_ENROLLMENT_ENABLED
                                or self.CLOSABLE_FLOW_NONPRODUCTION_ENROLLMENT_ENABLED)
                               and not self.CLOSABLE_FLOW_FORCE_CONTROL_DELIVERY,
        )
        if error:
            raise ValueError(f"Invalid closable-flow configuration: {error}")
        return self


settings = Settings()  # type: ignore

# Pinned MapLibre build for the flight-share page. Re-verify both hashes with
# `curl -s URL | openssl dgst -sha384 -binary | openssl base64 -A` on upgrade.
MAPLIBRE_JS_URL = "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/5.24.0/maplibre-gl.js"
MAPLIBRE_JS_INTEGRITY = "sha384-5+cfbwT0iiub6VsQAdn6yz16nr6sDiQoHx6tm4O8OVYXHYOxcffFmCJBL0dgdvGp"
MAPLIBRE_CSS_URL = "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/5.24.0/maplibre-gl.css"
MAPLIBRE_CSS_INTEGRITY = "sha384-uTttxo/aOKbdE5RlD/SPzSDoDmNvGlUYPjONi2MN/b7c9HPSvW07OIuyP7uL6jxK"
