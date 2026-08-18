import asyncio
import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import httpx
import jwt

from ..config import settings
from ..models.apple_ads import AppleAdsSpendDaily


class AppleAdsConfigurationError(RuntimeError):
    pass


class AppleAdsAPIError(RuntimeError):
    pass


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _metric_int(metrics: dict[str, Any], key: str) -> int:
    return _optional_int(metrics.get(key)) or 0


def _money_to_microunits(value: Any) -> tuple[int, str]:
    money = value if isinstance(value, dict) else {}
    try:
        amount = Decimal(str(money.get("amount", "0")))
    except InvalidOperation:
        amount = Decimal(0)
    microunits = int(
        (amount * Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    return microunits, str(money.get("currency") or "UNKNOWN").upper()


def parse_report_rows(
    *,
    response_payload: dict[str, Any],
    dimension_level: str,
) -> list[AppleAdsSpendDaily]:
    """Convert an Apple daily report page into deterministic database rows."""

    data = response_payload.get("data", response_payload)
    reporting = (
        data.get("reportingDataResponse", data) if isinstance(data, dict) else {}
    )
    source_rows = reporting.get("row", []) if isinstance(reporting, dict) else []
    parsed: list[AppleAdsSpendDaily] = []

    for source_row in source_rows:
        metadata = source_row.get("metadata") or {}
        campaign_id = _optional_int(metadata.get("campaignId"))
        if campaign_id is None:
            continue
        ad_group_id = _optional_int(metadata.get("adGroupId"))
        keyword_id = _optional_int(metadata.get("keywordId"))

        for metrics in source_row.get("granularity") or []:
            reporting_date = str(metrics.get("date") or "")[:10]
            if len(reporting_date) != 10:
                continue
            spend_microunits, currency = _money_to_microunits(
                metrics.get("localSpend")
            )
            identity = ":".join(
                [
                    dimension_level,
                    reporting_date,
                    str(campaign_id),
                    str(ad_group_id or 0),
                    str(keyword_id or 0),
                ]
            )
            row_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            parsed.append(
                AppleAdsSpendDaily(
                    id=row_id,
                    date=reporting_date,
                    dimension_level=dimension_level,
                    org_id=_optional_int(metadata.get("orgId")),
                    campaign_id=campaign_id,
                    campaign_name=metadata.get("campaignName"),
                    ad_group_id=ad_group_id,
                    ad_group_name=metadata.get("adGroupName"),
                    keyword_id=keyword_id,
                    keyword=metadata.get("keyword"),
                    match_type=metadata.get("matchType"),
                    currency=currency,
                    spend_microunits=spend_microunits,
                    impressions=_metric_int(metrics, "impressions"),
                    taps=_metric_int(metrics, "taps"),
                    tap_installs=_metric_int(metrics, "tapInstalls"),
                    total_installs=_metric_int(metrics, "totalInstalls"),
                    new_downloads=_metric_int(metrics, "newDownloads"),
                    redownloads=_metric_int(metrics, "redownloads"),
                )
            )
    return parsed


class AppleAdsClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._external_client = client
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._access_token: str | None = None
        self._access_token_expires_at = datetime.min.replace(tzinfo=timezone.utc)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if self._external_client is None:
            await self._client.aclose()

    @staticmethod
    def oauth_configured() -> bool:
        return all(
            [
                settings.APPLE_ADS_CLIENT_ID,
                settings.APPLE_ADS_TEAM_ID,
                settings.APPLE_ADS_KEY_ID,
                settings.APPLE_ADS_PRIVATE_KEY_PATH,
                settings.APPLE_ADS_ORG_ID,
            ]
        )

    async def exchange_attribution_token(
        self,
        attribution_token: str,
        *,
        retry_delay_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """Exchange the short-lived token without ever logging or persisting it."""

        for attempt in range(3):
            try:
                response = await self._client.post(
                    settings.APPLE_ADS_ATTRIBUTION_URL,
                    content=attribution_token,
                    headers={"Content-Type": "text/plain"},
                )
            except httpx.HTTPError as error:
                raise AppleAdsAPIError(
                    "AdServices attribution exchange was unreachable"
                ) from error
            if response.status_code == 404 and attempt < 2:
                await asyncio.sleep(retry_delay_seconds)
                continue
            if not 200 <= response.status_code < 300:
                raise AppleAdsAPIError(
                    f"AdServices attribution exchange failed ({response.status_code})"
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise AppleAdsAPIError(
                    "AdServices returned an invalid response"
                ) from error
            if not isinstance(payload, dict):
                raise AppleAdsAPIError("AdServices returned an invalid response")
            return payload
        raise AppleAdsAPIError("AdServices attribution exchange failed after retries")

    def _client_secret(self) -> str:
        if not self.oauth_configured():
            raise AppleAdsConfigurationError(
                "Apple Ads OAuth read credentials are not configured"
            )
        private_key_path = Path(str(settings.APPLE_ADS_PRIVATE_KEY_PATH))
        try:
            private_key = private_key_path.read_text(encoding="utf-8")
        except OSError as error:
            raise AppleAdsConfigurationError(
                "Apple Ads private key is unavailable"
            ) from error
        now = datetime.now(timezone.utc)
        try:
            return jwt.encode(
                {
                    "iss": settings.APPLE_ADS_TEAM_ID,
                    "iat": int(now.timestamp()),
                    "exp": int((now + timedelta(days=30)).timestamp()),
                    "aud": "https://appleid.apple.com",
                    "sub": settings.APPLE_ADS_CLIENT_ID,
                },
                private_key,
                algorithm="ES256",
                headers={"kid": settings.APPLE_ADS_KEY_ID},
            )
        except (jwt.PyJWTError, ValueError) as error:
            raise AppleAdsConfigurationError(
                "Apple Ads private key is invalid"
            ) from error

    async def _oauth_access_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token

        try:
            response = await self._client.post(
                "https://appleid.apple.com/auth/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.APPLE_ADS_CLIENT_ID,
                    "client_secret": self._client_secret(),
                    "scope": "searchadsorg",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as error:
            raise AppleAdsAPIError("Apple Ads OAuth was unreachable") from error
        if not 200 <= response.status_code < 300:
            raise AppleAdsAPIError(
                f"Apple Ads OAuth failed ({response.status_code})"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise AppleAdsAPIError("Apple Ads OAuth returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise AppleAdsAPIError("Apple Ads OAuth returned an invalid response")
        token = payload.get("access_token")
        if not token:
            raise AppleAdsAPIError("Apple Ads OAuth returned no access token")
        expires_in = int(payload.get("expires_in") or 3600)
        self._access_token = str(token)
        self._access_token_expires_at = now + timedelta(
            seconds=max(60, expires_in - 60)
        )
        return self._access_token

    async def _api_headers(self) -> dict[str, str]:
        token = await self._oauth_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "X-AP-Context": f"orgId={settings.APPLE_ADS_ORG_ID}",
            "Content-Type": "application/json",
        }

    async def campaign_ids(self) -> list[int]:
        headers = await self._api_headers()
        campaign_ids: list[int] = []
        offset = 0
        limit = 1000
        while True:
            try:
                response = await self._client.get(
                    f"{settings.APPLE_ADS_API_BASE_URL.rstrip('/')}/campaigns",
                    params={"offset": offset, "limit": limit},
                    headers=headers,
                )
            except httpx.HTTPError as error:
                raise AppleAdsAPIError(
                    "Apple Ads campaign fetch was unreachable"
                ) from error
            if not 200 <= response.status_code < 300:
                raise AppleAdsAPIError(
                    f"Apple Ads campaign fetch failed ({response.status_code})"
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise AppleAdsAPIError(
                    "Apple Ads campaign fetch returned invalid JSON"
                ) from error
            data = payload.get("data", []) if isinstance(payload, dict) else []
            if isinstance(data, dict):
                records = data.get("campaigns") or data.get("row") or []
            else:
                records = data
            page_ids = [
                campaign_id
                for record in records
                if (campaign_id := _optional_int(record.get("id"))) is not None
            ]
            campaign_ids.extend(page_ids)
            if len(records) < limit:
                break
            offset += limit
        return sorted(set(campaign_ids))

    async def _report_pages(
        self,
        *,
        campaign_id: int,
        dimension_level: str,
        start_date: date,
        end_date: date,
    ) -> list[AppleAdsSpendDaily]:
        endpoint = "adgroups" if dimension_level == "ad_group" else "keywords"
        order_by_field = (
            "adGroupId" if dimension_level == "ad_group" else "keywordId"
        )
        headers = await self._api_headers()
        rows: list[AppleAdsSpendDaily] = []
        offset = 0
        limit = 1000

        while True:
            body = {
                "startTime": start_date.isoformat(),
                "endTime": end_date.isoformat(),
                "granularity": "DAILY",
                "timeZone": "UTC",
                "returnRecordsWithNoMetrics": False,
                "returnRowTotals": False,
                "returnGrandTotals": False,
                "selector": {
                    "orderBy": [
                        {
                            "field": order_by_field,
                            "sortOrder": "ASCENDING",
                        }
                    ],
                    "pagination": {"offset": offset, "limit": limit},
                },
            }
            try:
                response = await self._client.post(
                    (
                        f"{settings.APPLE_ADS_API_BASE_URL.rstrip('/')}"
                        f"/reports/campaigns/{campaign_id}/{endpoint}"
                    ),
                    json=body,
                    headers=headers,
                )
            except httpx.HTTPError as error:
                raise AppleAdsAPIError(
                    f"Apple Ads {dimension_level} report was unreachable"
                ) from error
            if not 200 <= response.status_code < 300:
                raise AppleAdsAPIError(
                    f"Apple Ads {dimension_level} report failed "
                    f"for campaign {campaign_id} ({response.status_code})"
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise AppleAdsAPIError(
                    f"Apple Ads {dimension_level} report returned invalid JSON"
                ) from error
            if not isinstance(payload, dict):
                raise AppleAdsAPIError(
                    f"Apple Ads {dimension_level} report returned an invalid response"
                )
            page_rows = parse_report_rows(
                response_payload=payload,
                dimension_level=dimension_level,
            )
            rows.extend(page_rows)

            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            reporting = data.get("reportingDataResponse", data)
            raw_rows = reporting.get("row", []) if isinstance(reporting, dict) else []
            if len(raw_rows) < limit:
                break
            offset += limit
        return rows

    async def spend_rows(
        self,
        *,
        start_date: date,
        end_date: date,
        campaign_ids: list[int] | None = None,
    ) -> list[AppleAdsSpendDaily]:
        if (end_date - start_date).days > 89:
            raise ValueError("Apple daily reports support at most a 90-day window")
        ids = campaign_ids if campaign_ids is not None else await self.campaign_ids()
        rows: list[AppleAdsSpendDaily] = []
        for campaign_id in sorted(set(ids)):
            for dimension_level in ("ad_group", "keyword"):
                rows.extend(
                    await self._report_pages(
                        campaign_id=campaign_id,
                        dimension_level=dimension_level,
                        start_date=start_date,
                        end_date=end_date,
                    )
                )
        return rows
