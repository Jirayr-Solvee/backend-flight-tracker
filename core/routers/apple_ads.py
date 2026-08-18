import logging
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import Session, select

from ..dependency import check_lambda_auth_token, get_current_user
from ..models import get_session
from ..models.apple_ads import (
    AppleAdsAttribution,
    AppStoreRevenueEvent,
    current_time_ms,
)
from ..models.device import Device
from ..models.user import User
from ..services.apple_ads import (
    AppleAdsAPIError,
    AppleAdsClient,
    AppleAdsConfigurationError,
)
from ..services.apple_ads_reporting import build_measurement_report
from ..services.exchange_rates import ExchangeRateError, fetch_latest_ecb_rates
from ..services.revenue_measurement import backfill_verified_revenue_events


logger = logging.getLogger(__name__)
router = APIRouter()


class AttributionRequest(BaseModel):
    attribution_token: str = PydanticField(min_length=1, max_length=20_000)
    device_id: str = PydanticField(min_length=1, max_length=140)
    app_version: str = PydanticField(min_length=1, max_length=40)
    build_number: str = PydanticField(min_length=1, max_length=40)
    analytics_environment: Literal["production"]


class SpendSyncRequest(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    campaign_ids: list[int] | None = None


def _optional_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@router.post("/attribution")
async def record_attribution(
    data: AttributionRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    device = session.get(Device, data.device_id)
    if device is None or device.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Install does not belong to the authenticated user",
        )

    existing = session.get(AppleAdsAttribution, data.device_id)
    if existing:
        if existing.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Attribution ownership conflict",
            )
        return {"detail": "already_recorded", "attributed": existing.attributed}

    try:
        async with AppleAdsClient() as client:
            payload = await client.exchange_attribution_token(data.attribution_token)
    except AppleAdsAPIError as error:
        logger.warning(
            "Unable to exchange AdServices token user_id=%s device_id=%s: %s",
            user.id,
            data.device_id,
            error,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Apple attribution is temporarily unavailable",
        )

    now_ms = current_time_ms()
    org_id = _optional_int(payload.get("orgId"))
    campaign_id = _optional_int(payload.get("campaignId"))
    # Apple's developer-mode test payload uses zero-valued identifiers. Keep it
    # out of production acquisition cohorts even when it says attributed=true.
    is_production_attribution = bool(payload.get("attribution", False)) and bool(
        org_id and campaign_id
    )
    attribution = AppleAdsAttribution(
        id=data.device_id,
        user_id=user.id,
        attributed=is_production_attribution,
        org_id=org_id,
        campaign_id=campaign_id,
        ad_group_id=_optional_int(payload.get("adGroupId")),
        keyword_id=_optional_int(payload.get("keywordId")),
        ad_id=_optional_int(payload.get("adId")),
        country_or_region=payload.get("countryOrRegion"),
        placement=(str(payload["placement"]) if payload.get("placement") else None),
        conversion_type=(
            str(payload["conversionType"])
            if payload.get("conversionType") is not None
            else None
        ),
        claim_type=(
            str(payload["claimType"])
            if payload.get("claimType") is not None
            else None
        ),
        click_date=payload.get("clickDate"),
        impression_date=payload.get("impressionDate"),
        app_version=data.app_version,
        build_number=data.build_number,
        analytics_environment=data.analytics_environment,
        first_reported_at_ms=now_ms,
        updated_at_ms=now_ms,
    )
    session.add(attribution)
    session.commit()
    return {"detail": "recorded", "attributed": attribution.attributed}


@router.get("/status", dependencies=[Depends(check_lambda_auth_token)])
def measurement_status():
    return {
        "adservices_attribution_enabled": True,
        "campaign_api_configured": AppleAdsClient.oauth_configured(),
    }


@router.post("/spend/sync", dependencies=[Depends(check_lambda_auth_token)])
async def sync_spend(
    data: SpendSyncRequest,
    session: Session = Depends(get_session),
):
    end_date = data.end_date or datetime.now(timezone.utc).date()
    start_date = data.start_date or end_date - timedelta(days=29)
    if start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_date must not be after end_date",
        )
    if (end_date - start_date).days > 89:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Apple daily reports support at most a 90-day window",
        )

    try:
        async with AppleAdsClient() as client:
            rows = await client.spend_rows(
                start_date=start_date,
                end_date=end_date,
                campaign_ids=data.campaign_ids,
            )
    except AppleAdsConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        )
    except AppleAdsAPIError as error:
        logger.warning("Apple Ads spend sync failed: %s", error)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Apple Ads spend sync failed",
        )

    for row in rows:
        session.merge(row)
    session.commit()
    return {
        "detail": "success",
        "start_date": start_date,
        "end_date": end_date,
        "campaign_count": len({row.campaign_id for row in rows}),
        "row_count": len(rows),
    }


@router.get("/report", dependencies=[Depends(check_lambda_auth_token)])
async def measurement_report(
    start_date: date = Query(...),
    end_date: date = Query(...),
    dimension: Literal["campaign", "ad_group", "keyword"] = Query(
        default="campaign"
    ),
    session: Session = Depends(get_session),
):
    if start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_date must not be after end_date",
        )
    currencies = {
        event.currency
        for event in session.exec(select(AppStoreRevenueEvent)).all()
        if event.currency
    }
    try:
        exchange_rates = await fetch_latest_ecb_rates(currencies)
    except (ExchangeRateError, httpx.HTTPError) as error:
        logger.warning("Unable to fetch ECB reference rates: %s", error)
        exchange_rates = {"EUR": 1.0}

    return build_measurement_report(
        session=session,
        start_date=start_date,
        end_date=end_date,
        dimension=dimension,
        exchange_rates=exchange_rates,
    )


@router.post("/revenue/backfill", dependencies=[Depends(check_lambda_auth_token)])
def backfill_revenue(session: Session = Depends(get_session)):
    return {"detail": "success", **backfill_verified_revenue_events(session)}
