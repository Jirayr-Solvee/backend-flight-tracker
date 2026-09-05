from collections import defaultdict
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from sqlmodel import Session, select

from ..models.apple_ads import (
    AppleAdsAttribution,
    AppleAdsSpendDaily,
    AppStoreRevenueEvent,
)
from .revenue_measurement import refunded_milliunits


Dimension = Literal["campaign", "ad_group", "keyword"]
METRIC_FIELDS = (
    "spend_microunits",
    "impressions",
    "taps",
    "tap_installs",
    "total_installs",
    "new_downloads",
    "redownloads",
)


def _utc_ms(value: date) -> int:
    return int(datetime.combine(value, time.min, tzinfo=timezone.utc).timestamp() * 1000)


def _group_key(item: Any, dimension: Dimension) -> tuple[int | None, ...]:
    if dimension == "campaign":
        return (item.campaign_id,)
    if dimension == "ad_group":
        return (item.campaign_id, item.ad_group_id)
    return (item.campaign_id, item.ad_group_id, item.keyword_id)


def _empty_metrics() -> dict[str, int]:
    return {field: 0 for field in METRIC_FIELDS}


def _add_metrics(target: dict[str, int], source: AppleAdsSpendDaily, sign: int = 1):
    for field in METRIC_FIELDS:
        target[field] += int(getattr(source, field) or 0) * sign


def _amounts(
    values: dict[str, int], *, divisor: int
) -> dict[str, float]:
    return {
        currency: round(amount / divisor, 6)
        for currency, amount in sorted(values.items())
    }


def _single_currency_ratio(
    *,
    spend: dict[str, int],
    revenue: dict[str, int],
) -> tuple[float | None, str]:
    if not spend or sum(spend.values()) == 0:
        return None, "no_spend"
    if len(spend) != 1:
        return None, "multiple_spend_currencies"
    spend_currency, spend_value = next(iter(spend.items()))
    if not revenue:
        return 0.0, "complete"
    if set(revenue) != {spend_currency}:
        return None, "currency_conversion_required"
    return round(revenue[spend_currency] * 1000 / spend_value, 4), "complete"


def _normalize_revenue(
    *,
    spend: dict[str, int],
    revenue: dict[str, int],
    exchange_rates: dict[str, float],
) -> tuple[dict[str, int], str | None, str]:
    if len(spend) != 1:
        return revenue, None, "multiple_spend_currencies"
    target_currency = next(iter(spend))
    if not revenue or set(revenue) == {target_currency}:
        return revenue, target_currency, "native_currency"
    required = set(revenue) | {target_currency}
    if not required.issubset(exchange_rates):
        return revenue, target_currency, "missing_exchange_rate"

    target_rate = Decimal(str(exchange_rates[target_currency]))
    normalized = Decimal(0)
    for currency, amount in revenue.items():
        source_rate = Decimal(str(exchange_rates[currency]))
        normalized += Decimal(amount) / source_rate * target_rate
    return (
        {
            target_currency: int(
                normalized.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
        },
        target_currency,
        "ecb_reference_rate",
    )


def _single_currency_cost(
    spend: dict[str, int], denominator: int
) -> dict[str, float] | None:
    if denominator <= 0 or len(spend) != 1:
        return None
    currency, amount = next(iter(spend.items()))
    return {
        "amount": round(amount / 1_000_000 / denominator, 6),
        "currency": currency,
    }


def build_measurement_report(
    *,
    session: Session,
    start_date: date,
    end_date: date,
    dimension: Dimension,
    exchange_rates: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build an acquisition-cohort report from owned, server-verified data."""

    exchange_rates = exchange_rates or {}
    start_ms = _utc_ms(start_date)
    end_exclusive_ms = _utc_ms(date.fromordinal(end_date.toordinal() + 1))
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    attributions = session.exec(
        select(AppleAdsAttribution).where(
            AppleAdsAttribution.attributed == True,  # noqa: E712
            AppleAdsAttribution.analytics_environment == "production",
            AppleAdsAttribution.first_reported_at_ms >= start_ms,
            AppleAdsAttribution.first_reported_at_ms < end_exclusive_ms,
        )
    ).all()
    spend_rows = session.exec(
        select(AppleAdsSpendDaily).where(
            AppleAdsSpendDaily.date >= start_date.isoformat(),
            AppleAdsSpendDaily.date <= end_date.isoformat(),
        )
    ).all()

    metrics_by_group_currency: dict[
        tuple[int | None, ...], dict[str, dict[str, int]]
    ] = defaultdict(lambda: defaultdict(_empty_metrics))
    names: dict[tuple[int | None, ...], dict[str, Any]] = {}

    if dimension in ("campaign", "ad_group"):
        for row in spend_rows:
            # Ad-group totals are the non-overlapping source for both rollups.
            if row.dimension_level != "ad_group":
                continue
            key = _group_key(row, dimension)
            _add_metrics(metrics_by_group_currency[key][row.currency], row)
            names[key] = {
                "campaign_name": row.campaign_name,
                "ad_group_name": row.ad_group_name,
            }
    else:
        # Explicit keyword rows are exact. Search Match is the residual between
        # an ad group's total and the sum of its explicit keywords.
        search_match: dict[tuple[int | None, ...], dict[str, dict[str, int]]] = (
            defaultdict(lambda: defaultdict(_empty_metrics))
        )
        for row in spend_rows:
            key = _group_key(row, "keyword")
            if row.dimension_level == "ad_group":
                search_key = (row.campaign_id, row.ad_group_id, None)
                _add_metrics(search_match[search_key][row.currency], row)
                names[search_key] = {
                    "campaign_name": row.campaign_name,
                    "ad_group_name": row.ad_group_name,
                    "keyword": "Search Match",
                    "match_type": "AUTO",
                }
            elif row.dimension_level == "keyword":
                _add_metrics(metrics_by_group_currency[key][row.currency], row)
                search_key = (row.campaign_id, row.ad_group_id, None)
                _add_metrics(search_match[search_key][row.currency], row, sign=-1)
                names[key] = {
                    "campaign_name": row.campaign_name,
                    "ad_group_name": row.ad_group_name,
                    "keyword": row.keyword,
                    "match_type": row.match_type,
                }
        for key, currencies in search_match.items():
            for currency, metrics in currencies.items():
                for field in METRIC_FIELDS:
                    metrics[field] = max(0, metrics[field])
                metrics_by_group_currency[key][currency] = metrics

    stats: dict[tuple[int | None, ...], dict[str, Any]] = defaultdict(
        lambda: {
            "install_ids": set(),
            "user_ids": set(),
            "trial_user_ids": set(),
            "payer_user_ids": set(),
            "purchase_count": 0,
            "renewal_count": 0,
            "refunded_transaction_count": 0,
            "gross_revenue": defaultdict(int),
            "net_revenue": defaultdict(int),
            "net_revenue_d0": defaultdict(int),
            "net_revenue_d7": defaultdict(int),
            "net_revenue_d30": defaultdict(int),
            "matured_d7_install_ids": set(),
            "matured_d30_install_ids": set(),
        }
    )
    attributions_by_user: dict[str, list[AppleAdsAttribution]] = defaultdict(list)
    for attribution in attributions:
        key = _group_key(attribution, dimension)
        stats[key]["install_ids"].add(attribution.id)
        stats[key]["user_ids"].add(attribution.user_id)
        attributions_by_user[attribution.user_id].append(attribution)
        if now_ms - attribution.first_reported_at_ms >= 7 * 86_400_000:
            stats[key]["matured_d7_install_ids"].add(attribution.id)
        if now_ms - attribution.first_reported_at_ms >= 30 * 86_400_000:
            stats[key]["matured_d30_install_ids"].add(attribution.id)

    for values in attributions_by_user.values():
        values.sort(key=lambda item: item.first_reported_at_ms)

    user_ids = set(attributions_by_user)
    revenue_events = []
    if user_ids:
        revenue_events = session.exec(
            select(AppStoreRevenueEvent).where(
                AppStoreRevenueEvent.user_id.in_(user_ids),
                AppStoreRevenueEvent.purchase_environment == "Production",
            )
        ).all()

    for event in revenue_events:
        candidates = [
            attribution
            for attribution in attributions_by_user[event.user_id]
            if attribution.first_reported_at_ms <= event.purchase_date_ms
        ]
        if not candidates:
            continue
        attribution = candidates[-1]
        key = _group_key(attribution, dimension)
        group = stats[key]
        currency = event.currency.upper()
        gross = max(0, event.price_milliunits)
        if event.revoked_date_ms:
            net = gross - refunded_milliunits(gross, event.revocation_percentage)
            group["refunded_transaction_count"] += 1
        else:
            net = gross

        group["gross_revenue"][currency] += gross
        group["net_revenue"][currency] += net
        if event.starts_trial:
            group["trial_user_ids"].add(event.user_id)
        if net > 0:
            group["payer_user_ids"].add(event.user_id)
        group["purchase_count"] += 1
        if event.transaction_reason == "RENEWAL":
            group["renewal_count"] += 1

        age_ms = max(0, event.purchase_date_ms - attribution.first_reported_at_ms)
        if age_ms < 86_400_000:
            group["net_revenue_d0"][currency] += net
        if age_ms < 7 * 86_400_000:
            group["net_revenue_d7"][currency] += net
        if age_ms < 30 * 86_400_000:
            group["net_revenue_d30"][currency] += net

    all_keys = set(metrics_by_group_currency) | set(stats)
    result_rows = []
    for key in sorted(all_keys, key=lambda value: tuple(-1 if v is None else v for v in value)):
        group = stats[key]
        currency_metrics = metrics_by_group_currency[key]
        spend = {
            currency: metrics["spend_microunits"]
            for currency, metrics in currency_metrics.items()
        }
        combined_metrics = _empty_metrics()
        for metrics in currency_metrics.values():
            for field in METRIC_FIELDS:
                combined_metrics[field] += metrics[field]

        normalized_d0, reporting_currency, fx_status = _normalize_revenue(
            spend=spend,
            revenue=group["net_revenue_d0"],
            exchange_rates=exchange_rates,
        )
        normalized_d7, _, fx_status_d7 = _normalize_revenue(
            spend=spend,
            revenue=group["net_revenue_d7"],
            exchange_rates=exchange_rates,
        )
        normalized_d30, _, fx_status_d30 = _normalize_revenue(
            spend=spend,
            revenue=group["net_revenue_d30"],
            exchange_rates=exchange_rates,
        )
        normalized_lifetime, _, fx_status_lifetime = _normalize_revenue(
            spend=spend,
            revenue=group["net_revenue"],
            exchange_rates=exchange_rates,
        )
        roas_d0, roas_d0_status = _single_currency_ratio(
            spend=spend, revenue=normalized_d0
        )
        roas_d7, roas_d7_status = _single_currency_ratio(
            spend=spend, revenue=normalized_d7
        )
        roas_d30, roas_d30_status = _single_currency_ratio(
            spend=spend, revenue=normalized_d30
        )
        install_count = len(group["install_ids"])
        payer_count = len(group["payer_user_ids"])
        trial_count = len(group["trial_user_ids"])
        identity = {
            "campaign_id": key[0],
            "ad_group_id": key[1] if len(key) > 1 else None,
            "keyword_id": key[2] if len(key) > 2 else None,
            **names.get(key, {}),
        }
        result_rows.append(
            {
                **identity,
                "spend": _amounts(spend, divisor=1_000_000),
                "impressions": combined_metrics["impressions"],
                "taps": combined_metrics["taps"],
                "apple_reported_installs": combined_metrics["total_installs"],
                "attributed_installs": install_count,
                "attributed_users": len(group["user_ids"]),
                "verified_trial_users": trial_count,
                "paying_customers": payer_count,
                "verified_transactions": group["purchase_count"],
                "renewals": group["renewal_count"],
                "refunded_transactions": group["refunded_transaction_count"],
                "gross_revenue": _amounts(
                    group["gross_revenue"], divisor=1_000
                ),
                "net_revenue": _amounts(group["net_revenue"], divisor=1_000),
                "net_revenue_d0": _amounts(
                    group["net_revenue_d0"], divisor=1_000
                ),
                "net_revenue_d7": _amounts(
                    group["net_revenue_d7"], divisor=1_000
                ),
                "net_revenue_d30": _amounts(
                    group["net_revenue_d30"], divisor=1_000
                ),
                "normalized_net_revenue": _amounts(
                    normalized_lifetime, divisor=1_000
                ),
                "roas_reporting_currency": reporting_currency,
                "fx_status": fx_status_lifetime,
                "cost_per_attributed_install": _single_currency_cost(
                    spend, install_count
                ),
                "cost_per_verified_trial": _single_currency_cost(
                    spend, trial_count
                ),
                "cac": _single_currency_cost(spend, payer_count),
                "roas_d0": roas_d0,
                "roas_d7": roas_d7,
                "roas_d30": roas_d30,
                "roas_d0_status": (
                    fx_status if roas_d0_status == "complete" else roas_d0_status
                ),
                "roas_d7_status": (
                    fx_status_d7
                    if roas_d7_status == "complete"
                    else roas_d7_status
                ),
                "roas_d30_status": (
                    fx_status_d30
                    if roas_d30_status == "complete"
                    else roas_d30_status
                ),
                "matured_d7_installs": len(group["matured_d7_install_ids"]),
                "matured_d30_installs": len(group["matured_d30_install_ids"]),
            }
        )

    return {
        "cohort_start_date": start_date.isoformat(),
        "cohort_end_date": end_date.isoformat(),
        "dimension": dimension,
        "analytics_environment": "production",
        "purchase_environment": "Production",
        "currency_policy": (
            "Revenue is converted to the row's spend currency using the latest "
            "available ECB reference rate. Original per-currency amounts remain "
            "available, and ROAS stays null when a required rate is unavailable."
        ),
        "exchange_rate_source": "ECB latest reference rate",
        "rows": result_rows,
    }
