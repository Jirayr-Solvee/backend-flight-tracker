from enum import Enum

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


settings = Settings()  # type: ignore
