"""Installation cohort reporting from independently verified purchase facts."""

from collections import defaultdict

from sqlmodel import Session, select

from ..models.apple_ads import AppleAdsAttribution, AppStoreRevenueEvent
from ..models.experiment import (
    ExperimentConversion, ExperimentEnrollment, ExperimentExposure, current_time_ms,
)


def experiment_summary(
    *, session: Session, experiment_id: str, app_version: str | None,
    measurement_revision: int, since_ms: int | None, until_ms: int | None,
    product_id: str | None, acquisition_source: str | None,
    horizon_days: int, as_of_ms: int | None = None,
) -> dict:
    now_ms = as_of_ms if as_of_ms is not None else current_time_ms()
    exposures = session.exec(select(ExperimentExposure).where(
        ExperimentExposure.experiment_id == experiment_id,
        ExperimentExposure.eligible == True,  # noqa: E712
        ExperimentExposure.analytics_environment == "production",
    )).all()
    enrollments = session.exec(select(ExperimentEnrollment).where(
        ExperimentEnrollment.experiment_id == experiment_id,
        ExperimentEnrollment.eligible == True,  # noqa: E712
        ExperimentEnrollment.analytics_environment == "production",
    )).all()
    enrolled_ids = {item.installation_id for item in enrollments}
    # v1 and v2 denominators must never be pooled: v1 starts at the paywall;
    # v2 starts before the two onboarding paths diverge.
    cohort = enrollments if measurement_revision == 2 else [
        item for item in exposures if item.installation_id not in enrolled_ids
    ]
    ads = session.exec(select(AppleAdsAttribution).where(
        AppleAdsAttribution.analytics_environment == "production",
        AppleAdsAttribution.attributed == True,  # noqa: E712
    )).all()
    attributed_users = {item.user_id for item in ads}

    def source(item):
        return "apple_ads" if item.user_id in attributed_users else "unknown"

    def cohort_time(item):
        return item.enrolled_at_ms if measurement_revision == 2 else item.exposed_at_ms

    cohort = [item for item in cohort if (
        (app_version is None or item.app_version == app_version)
        and (since_ms is None or cohort_time(item) >= since_ms)
        and (until_ms is None or cohort_time(item) < until_ms)
        and cohort_time(item) <= now_ms
        and (acquisition_source is None or source(item) == acquisition_source)
    )]
    conversions = session.exec(select(ExperimentConversion).where(
        ExperimentConversion.experiment_id == experiment_id,
        ExperimentConversion.eligible == True,  # noqa: E712
        ExperimentConversion.analytics_environment == "production",
        ExperimentConversion.purchase_environment == "Production",
    )).all()
    revenue = session.exec(select(AppStoreRevenueEvent).where(
        AppStoreRevenueEvent.purchase_environment == "Production",
        AppStoreRevenueEvent.purchase_date_ms <= now_ms,
    )).all()
    revenue_by_subscription = defaultdict(list)
    revenue_by_id = {item.id: item for item in revenue}
    for item in revenue:
        revenue_by_subscription[item.original_transaction_id].append(item)

    arms = []
    for variant in sorted({item.variant for item in cohort}):
        members = {item.installation_id: item for item in cohort if item.variant == variant}
        n = len(members)
        all_arm_conversions = [item for item in conversions if (
            item.installation_id in members and item.variant == variant
            and item.purchase_date_ms is not None
            and cohort_time(members[item.installation_id]) <= item.purchase_date_ms <= now_ms
        )]
        arm_conversions = [item for item in all_arm_conversions
                           if product_id is None or item.product_id == product_id]
        # A transaction may have been delivered by both StoreKit and server
        # notifications. Join by original transaction; dedup by Apple ID.
        events_by_installation = defaultdict(dict)
        for conversion in all_arm_conversions:
            for event in revenue_by_subscription[conversion.original_transaction_id]:
                if (
                    event.purchase_date_ms >= cohort_time(members[conversion.installation_id])
                    and (product_id is None or event.product_id == product_id)
                ):
                    events_by_installation[conversion.installation_id][event.id] = event
        trial_ids = {item.installation_id for item in arm_conversions if item.starts_trial}
        transaction_ids = {item.installation_id for item in arm_conversions}
        paid_ids = set()
        matured_trial_ids = set()
        unknown_trial_maturity_ids = set()
        matured_trial_paid_ids = set()
        paid_trial_ids = set()
        per_product = defaultdict(lambda: {"trial_installations": set(), "paid_installations": set()})
        for conversion in arm_conversions:
            if conversion.starts_trial:
                per_product[conversion.product_id]["trial_installations"].add(conversion.installation_id)
                trial_event = revenue_by_id.get(conversion.id)
                expires_ms = trial_event.expires_date_ms if trial_event else None
                if expires_ms is None and conversion.trial_duration_days:
                    expires_ms = conversion.purchase_date_ms + conversion.trial_duration_days * 86_400_000
                if expires_ms is None:
                    unknown_trial_maturity_ids.add(conversion.installation_id)
                elif expires_ms <= now_ms:
                    matured_trial_ids.add(conversion.installation_id)
                # The first charge after the verified free trial must be a
                # later transaction, not merely a nontrial offer classification.
                if any(event.price_milliunits > 0 and not event.starts_trial
                       and event.purchase_date_ms > conversion.purchase_date_ms
                       for event in events_by_installation[conversion.installation_id].values()):
                    paid_trial_ids.add(conversion.installation_id)
                    if expires_ms is not None and expires_ms <= now_ms:
                        matured_trial_paid_ids.add(conversion.installation_id)

        for installation_id, events in events_by_installation.items():
            for event in events.values():
                if event.price_milliunits > 0 and not event.starts_trial:
                    paid_ids.add(installation_id)
                    per_product[event.product_id]["paid_installations"].add(installation_id)

        horizon_ms = horizon_days * 86_400_000
        mature_members = {key for key, value in members.items()
                          if cohort_time(value) + horizon_ms <= now_ms}
        monetary = defaultdict(lambda: {"gross_milliunits": 0, "refund_milliunits": 0})
        seen_revenue = set()
        for installation_id in mature_members:
            end_ms = cohort_time(members[installation_id]) + horizon_ms
            for event in events_by_installation[installation_id].values():
                if (event.id in seen_revenue or event.purchase_date_ms >= end_ms
                        or event.starts_trial or event.price_milliunits <= 0):
                    continue
                seen_revenue.add(event.id)
                monetary[event.currency]["gross_milliunits"] += event.price_milliunits
                if event.revoked_date_ms is not None and event.revoked_date_ms <= now_ms:
                    fraction = min(100, max(0, event.revocation_percentage)) / 100 if event.revocation_percentage is not None else 1
                    monetary[event.currency]["refund_milliunits"] += round(event.price_milliunits * fraction)

        ratio = lambda value, denominator=n: round(value / denominator, 4) if denominator else None
        actual_paywall_ids = {item.installation_id for item in exposures
                             if item.variant == variant and item.source == "onboarding_exposure"
                             and item.installation_id in members}
        arms.append({
            "variant": variant,
            "eligible_installations": n,
            "exposed_installations": n,  # legacy response alias, see definitions
            "actual_paywall_exposed_installations": len(actual_paywall_ids) if measurement_revision == 2 else None,
            "verified_trial_installations": len(trial_ids),
            "verified_transaction_installations": len(transaction_ids),
            "verified_purchase_installations": len(transaction_ids),  # compatibility
            "paid_installations": len(paid_ids),
            "trial_conversion_rate": ratio(len(trial_ids)),
            "verified_transaction_conversion_rate": ratio(len(transaction_ids)),
            "purchase_conversion_rate": ratio(len(transaction_ids)),  # compatibility
            "paid_conversion_rate": ratio(len(paid_ids)),
            "matured_trial_installations": len(matured_trial_ids),
            "trial_maturity_unknown_installations": len(unknown_trial_maturity_ids),
            "trial_to_paid_installations": len(paid_trial_ids),
            "matured_trial_to_paid_installations": len(matured_trial_paid_ids),
            "matured_trial_to_paid_rate": ratio(len(matured_trial_paid_ids), len(matured_trial_ids)),
            "acquisition_sources": {name: sum(source(item) == name for item in members.values())
                                    for name in ("apple_ads", "unknown")},
            "products": [{"product_id": key,
                          "trial_installations": len(value["trial_installations"]),
                          "paid_installations": len(value["paid_installations"])}
                         for key, value in sorted(per_product.items())],
            "mature_horizon_installations": len(mature_members),
            "mature_horizon_revenue": [{
                "currency": currency,
                "gross_revenue": values["gross_milliunits"] / 1000,
                "refunds": values["refund_milliunits"] / 1000,
                "revenue_after_refunds": (values["gross_milliunits"] - values["refund_milliunits"]) / 1000,
                "revenue_after_refunds_per_eligible_installation": round(
                    (values["gross_milliunits"] - values["refund_milliunits"]) / 1000 / len(mature_members), 4),
            } for currency, values in sorted(monetary.items())],
        })
    return {
        "experiment_id": experiment_id, "app_version": app_version,
        "analytics_environment": "production", "purchase_environment": "Production",
        "measurement_revision": measurement_revision,
        "denominator": "selected_flight_enrollment" if measurement_revision == 2 else "legacy_paywall_exposure",
        "since_ms": since_ms, "until_ms": until_ms, "as_of_ms": now_ms,
        "product_id": product_id, "acquisition_source": acquisition_source,
        "maturity_horizon_days": horizon_days,
        "definitions": {
            "purchase_conversion_rate": "Deprecated alias of verified_transaction_conversion_rate; includes free trials and is not paid conversion.",
            "verified_purchase_installations": "Deprecated alias of verified_transaction_installations; includes free trials.",
            "exposed_installations": "Compatibility alias of eligible_installations; use denominator to identify cohort stage.",
            "paid_conversion_rate": "Verified positive-price nontrial transaction, including renewals, per eligible installation. Refunds reported separately.",
            "unknown_acquisition": "No verified Apple Ads attribution. Not evidence of organic or non-Meta acquisition.",
            "apple_ads_acquisition": "Verified Apple Ads attribution joined by Sofly user ID; a user-level join, not proof of the source of each reinstall.",
            "product_filter": "Filters outcomes, never conditions the eligible denominator on having purchased a product.",
            "revenue": "Verified gross customer payments less known refunds; not Apple developer proceeds. Currencies are not combined.",
        },
        "arms": arms,
    }
