from time import time

from sqlmodel import Field, SQLModel


def current_time_ms() -> int:
    return int(time() * 1_000)


class AppleAdsAttribution(SQLModel, table=True):
    """One server-verified AdServices attribution record per Sofly install."""

    id: str = Field(primary_key=True, description="Stable Sofly device/install ID")
    user_id: str = Field(index=True)
    attributed: bool = Field(index=True)

    org_id: int | None = Field(default=None, index=True)
    campaign_id: int | None = Field(default=None, index=True)
    ad_group_id: int | None = Field(default=None, index=True)
    keyword_id: int | None = Field(default=None, index=True)
    ad_id: int | None = None

    country_or_region: str | None = Field(default=None, index=True)
    placement: str | None = None
    conversion_type: str | None = None
    claim_type: str | None = None
    click_date: str | None = None
    impression_date: str | None = None

    app_version: str
    build_number: str
    analytics_environment: str = Field(index=True)
    first_reported_at_ms: int = Field(default_factory=current_time_ms, index=True)
    updated_at_ms: int = Field(default_factory=current_time_ms)


class AppleAdsSpendDaily(SQLModel, table=True):
    """Daily Apple Ads metrics at ad-group or explicit-keyword level."""

    id: str = Field(primary_key=True)
    date: str = Field(index=True, description="UTC reporting date (YYYY-MM-DD)")
    dimension_level: str = Field(index=True)

    org_id: int | None = Field(default=None, index=True)
    campaign_id: int = Field(index=True)
    campaign_name: str | None = None
    ad_group_id: int | None = Field(default=None, index=True)
    ad_group_name: str | None = None
    keyword_id: int | None = Field(default=None, index=True)
    keyword: str | None = None
    match_type: str | None = None

    currency: str = Field(index=True)
    spend_microunits: int = 0
    impressions: int = 0
    taps: int = 0
    tap_installs: int = 0
    total_installs: int = 0
    new_downloads: int = 0
    redownloads: int = 0
    updated_at_ms: int = Field(default_factory=current_time_ms)


class AppStoreRevenueEvent(SQLModel, table=True):
    """Verified StoreKit transaction facts used for cohort revenue and ROAS."""

    id: str = Field(
        primary_key=True,
        foreign_key="transaction.id",
        description="Verified App Store transaction ID",
    )
    original_transaction_id: str = Field(index=True)
    user_id: str = Field(index=True)
    product_id: str
    purchase_date_ms: int = Field(index=True)
    original_purchase_date_ms: int | None = None
    signed_date_ms: int | None = None
    expires_date_ms: int | None = None
    revoked_date_ms: int | None = Field(default=None, index=True)
    revocation_percentage: int | None = None
    transaction_reason: str | None = Field(default=None, index=True)
    purchase_environment: str = Field(index=True)
    price_milliunits: int = 0
    currency: str = Field(index=True)
    app_account_token: str | None = None
    offer_discount_type: str | None = Field(default=None, index=True)
    offer_period: str | None = None
    starts_trial: bool = Field(default=False, index=True)
    updated_at_ms: int = Field(default_factory=current_time_ms)
