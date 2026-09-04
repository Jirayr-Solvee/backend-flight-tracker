from enum import Enum
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class JWSEnvironment(str, Enum):
    XCODE = "XCODE"
    SANDBOX = "SANDBOX"
    PRODUCTION = "PRODUCTION"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_BUCKET_NAME: str
    AWS_REGION: str

    LAMBDA_FUNCTION_AUTH_TOKEN: str

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


settings = Settings()  # type: ignore
