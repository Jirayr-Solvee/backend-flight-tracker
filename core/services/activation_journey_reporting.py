"""Read-only, mature all-entrant reporting for the separate full-journey protocol.

No legacy cohort edits or inferred historical enrollments. All lists are bounded;
if a cap is hit no rates are returned from a silently incomplete extraction.
"""

import json
import math
from collections import Counter, defaultdict

from fastapi import HTTPException
from sqlalchemy import func, literal, union_all
from sqlmodel import select

from ..activation_journey_contract import JOURNEY_ID, JOURNEY_SCOPES, JOURNEY_VARIANTS
from ..models.activation_journey import (
    ActivationJourneyAttribution, ActivationJourneyDiagnosticContext, ActivationJourneyEnrollment,
    ActivationJourneyGoalSelection, ActivationJourneySelection,
)
from ..models.apple_ads import AppStoreRevenueEvent
from ..models.experiment import ExperimentDiagnosticEvent, ExperimentEnrollment, ExperimentExposure, current_time_ms
from .revenue_measurement import refunded_milliunits

DAY_MS = 86_400_000
ENTRY_CAP = 20_000
FACT_CAP = 100_000


def _rate(numerator, denominator):
    if not denominator:
        return {"numerator": numerator, "denominator": denominator, "rate": None, "wilson_95": None}
    p = numerator / denominator
    z = 1.959963984540054
    center = (p + z * z / (2 * denominator)) / (1 + z * z / denominator)
    half = z * math.sqrt(p * (1 - p) / denominator + z * z / (4 * denominator * denominator)) / (1 + z * z / denominator)
    return {"numerator": numerator, "denominator": denominator, "rate": p,
            "wilson_95": [max(0, center - half), min(1, center + half)]}


def _first(rows, predicate):
    return next((row for row in sorted(rows, key=lambda row: (row.purchase_date_ms, row.id)) if predicate(row)), None)


def _is_trial(row):
    return row.starts_trial and row.price_milliunits == 0


def _is_paid(row):
    return not row.starts_trial and row.price_milliunits > 0


def _purchased_checkout(fact, events):
    """Join an Apple fact to its actual frozen checkout, not first discovery.

    A pending/interrupted terminal may precede delayed verification, so require
    the separately verified transaction event and initiation, not an invented
    replacement terminal. Missing or contradictory joins stay unavailable.
    """
    candidates = set()
    for event, props in events:
        if event.event_name not in ("af_start_trial", "af_purchase") or props.get("transaction_id") != fact.id:
            continue
        if not event.checkout_attempt_id or not event.paywall_presentation_id or props.get("product_id") != fact.product_id or props.get("purchase_environment") != fact.purchase_environment:
            continue
        key = (event.checkout_attempt_id, event.paywall_presentation_id, props.get("paywall_surface"), props.get("flight_identity"))
        initiation = any(start.event_name == "af_initiated_checkout" and start.checkout_attempt_id == key[0]
                         and start.paywall_presentation_id == key[1] and start.occurred_at_ms <= event.occurred_at_ms
                         and values.get("product_id") == fact.product_id and values.get("paywall_surface") == key[2]
                         and values.get("flight_identity") == key[3] for start, values in events)
        if initiation:
            candidates.add(key)
    if len(candidates) != 1:
        return "conflicting_checkout_attribution" if candidates else "missing_checkout_attribution", None
    _, _, surface, identity = candidates.pop()
    if surface in ("skip_flight", "other") and identity is None:
        return "not_selected_flight_purchase", None
    if surface != "selected_flight" or not identity:
        return "missing_checkout_attribution", None
    return "selected_flight_purchase", identity


def journey_summary(*, session, analytics_environment="production", since_ms=None, until_ms=None,
                    as_of_ms=None, app_version=None, build_number=None):
    now = current_time_ms()
    cutoff = now if as_of_ms is None else as_of_ms
    if cutoff < 0 or cutoff > now + 300_000 or (since_ms is not None and until_ms is not None and since_ms >= until_ms):
        raise HTTPException(status_code=422, detail="Invalid journey report window")
    statement = select(ActivationJourneyEnrollment).where(
        ActivationJourneyEnrollment.experiment_id == JOURNEY_ID,
        ActivationJourneyEnrollment.measurement_revision == 1,
        ActivationJourneyEnrollment.analytics_environment == analytics_environment,
        ActivationJourneyEnrollment.enrolled_at_ms <= cutoff,
        ActivationJourneyEnrollment.first_reported_at_ms <= cutoff,
    )
    for field, value in ((ActivationJourneyEnrollment.app_version, app_version), (ActivationJourneyEnrollment.build_number, build_number)):
        if value is not None:
            statement = statement.where(field == value)
    if since_ms is not None:
        statement = statement.where(ActivationJourneyEnrollment.enrolled_at_ms >= since_ms)
    if until_ms is not None:
        statement = statement.where(ActivationJourneyEnrollment.enrolled_at_ms < until_ms)
    entries = session.exec(statement.order_by(ActivationJourneyEnrollment.id).limit(ENTRY_CAP + 1)).all()
    # Subqueries avoid SQLite bind-variable limits for large cohorts.
    cohort_ids = statement.with_only_columns(ActivationJourneyEnrollment.id).limit(ENTRY_CAP)
    event_query = select(ExperimentDiagnosticEvent, ActivationJourneyDiagnosticContext).join(
        ActivationJourneyDiagnosticContext, ActivationJourneyDiagnosticContext.id == ExperimentDiagnosticEvent.id,
    ).where(ActivationJourneyDiagnosticContext.enrollment_id.in_(cohort_ids),
            ExperimentDiagnosticEvent.occurred_at_ms <= cutoff,
            ExperimentDiagnosticEvent.received_at_ms <= cutoff)
    events = session.exec(event_query.order_by(ExperimentDiagnosticEvent.id).limit(FACT_CAP + 1)).all()
    attributes = session.exec(select(ActivationJourneyAttribution).where(
        ActivationJourneyAttribution.enrollment_id.in_(cohort_ids),
        ActivationJourneyAttribution.attributed_at_ms <= cutoff,
    ).limit(FACT_CAP + 1)).all()
    attribute_ids = select(ActivationJourneyAttribution.id).where(
        ActivationJourneyAttribution.enrollment_id.in_(cohort_ids),
        ActivationJourneyAttribution.attributed_at_ms <= cutoff,
    )
    revenue = session.exec(select(AppStoreRevenueEvent).where(
        AppStoreRevenueEvent.original_transaction_id.in_(attribute_ids),
        AppStoreRevenueEvent.purchase_date_ms <= cutoff,
    ).order_by(AppStoreRevenueEvent.id).limit(FACT_CAP + 1)).all()
    completeness = {
        "retained_extraction_complete": len(entries) <= ENTRY_CAP and len(events) <= FACT_CAP and len(attributes) <= FACT_CAP and len(revenue) <= FACT_CAP,
        "entry_cap": ENTRY_CAP, "fact_cap": FACT_CAP,
        "client_delivery_complete": "unproven",
        "diagnostic_retention_days": 90,
        "diagnostic_earliest_guaranteed_retention_ms": now - 90 * DAY_MS,
        "financial_as_of_semantics": "Purchase-time cutoff with currently retained verified refund facts; not a historical database snapshot. Late attribution may revise prior cohorts.",
    }
    result = {
        "experiment_id": JOURNEY_ID, "measurement_revision": 1,
        "extracted_at_ms": now, "as_of_ms": cutoff, "entry_start_inclusive_ms": since_ms,
        "entry_end_exclusive_ms": until_ms, "app_version": app_version, "build_number": build_number,
        "analytics_environment": analytics_environment, "completeness": completeness,
        "comparison": "Distinct immutable full-journey variants, not isolated onboarding, price, or layout effects. Compare concurrent matching entry/config windows; variant rows may include different launch periods.",
        "baseline_history": {"status": "unavailable_until_delivery_compatibility_audited",
            "manifest_endpoint": "/subscriptions/activation-journey/baseline-manifest",
            "reason": "Legacy onboarding and selected-flight entries have different denominators. No historical assignment, ten-minute selection, or matching offer/layout is inferred."},
        "existing_paywall_test": {"report_endpoint": "/subscriptions/experiments/paywall_flight_detail_2026_09/summary",
            "status": "Separate unchanged legacy report; preserve revision, build and existing public/frozen monitor windows."},
        "component_learning": {"status": "descriptive_historical_only",
            "warning": "Deduplicate installations and prove compatible delivery/entry horizons before comparing. Post-selection populations across different onboardings are selected, not randomized paywall comparisons."},
    }
    if not completeness["retained_extraction_complete"]:
        return {**result, "arms": [], "quality": {"status": "unavailable_extraction_cap_exceeded"}}
    by_id = {row.id: row for row in entries}
    events_by_entry = defaultdict(list)
    mismatched_context = 0
    for event, association in events:
        entry = by_id.get(association.enrollment_id)
        if not entry or association.user_id != entry.user_id or association.context_json != entry.context_json:
            mismatched_context += 1
            continue
        events_by_entry[entry.id].append((event, json.loads(event.properties_json)))
    attrs_by_original = {row.id: row for row in attributes}
    facts_by_entry = defaultdict(list)
    mismatched_financial = 0
    expected_env = {"production": {"Production"}, "testflight": {"Sandbox"}, "development": {"Sandbox", "Xcode"}}[analytics_environment]
    for fact in revenue:
        association = attrs_by_original[fact.original_transaction_id]
        entry = by_id[association.enrollment_id]
        # The immutable attribution retains original ownership even if a restore
        # subsequently refreshes the financial row's current linked user.
        if association.context_json != entry.context_json or association.user_id != entry.user_id or fact.purchase_environment not in expected_env or fact.purchase_date_ms < entry.enrolled_at_ms:
            mismatched_financial += 1
            continue
        facts_by_entry[entry.id].append(fact)
    selections = {row.id: row for row in session.exec(select(ActivationJourneySelection).where(
        ActivationJourneySelection.id.in_(cohort_ids), ActivationJourneySelection.first_reported_at_ms <= cutoff,
    )).all()}
    all_goal_rows = session.exec(select(ActivationJourneyGoalSelection).where(ActivationJourneyGoalSelection.id.in_(cohort_ids))).all()
    goal_rows = {row.id: row for row in all_goal_rows if row.reported_at_ms <= cutoff and row.selected_at_ms <= cutoff}
    later_goal_ids = {row.id for row in all_goal_rows if row.id not in goal_rows}
    randomized = [row for row in entries if row.eligible and row.randomized and row.assignment_source == "server_assignment"]
    fallback_counts = Counter((row.assignment_source, row.variant) for row in entries if row not in randomized)
    arms = []
    for variant in JOURNEY_VARIANTS:
        arm = [row for row in randomized if row.variant == variant]
        mature10 = [row for row in arm if row.enrolled_at_ms + 600_000 <= cutoff]
        mature24 = [row for row in arm if row.enrolled_at_ms + DAY_MS <= cutoff]
        def event_within(entry, name, horizon, predicate=lambda properties: True):
            return any(event.event_name == name and entry.enrolled_at_ms <= event.occurred_at_ms <= entry.enrolled_at_ms + horizon and predicate(properties)
                       for event, properties in events_by_entry[entry.id])
        def first_activation(entry):
            return _first(facts_by_entry[entry.id], lambda fact: _is_trial(fact) or _is_paid(fact))
        conditional = [row for row in arm if row.id in selections and selections[row.id].selected_at_ms + DAY_MS <= cutoff]
        activation_counts = Counter()
        purchased_selected = []
        for row in arm:
            fact = first_activation(row)
            if not fact or fact.purchase_date_ms + DAY_MS > cutoff:
                continue
            status, identity = _purchased_checkout(fact, events_by_entry[row.id])
            activation_counts[status] += 1
            if status == "selected_flight_purchase":
                purchased_selected.append((row, fact, identity))
        for row, fact, identity in purchased_selected:
            matching = [(event, props) for event, props in events_by_entry[row.id]
                        if fact.purchase_date_ms <= event.occurred_at_ms <= fact.purchase_date_ms + DAY_MS and props.get("flight_identity") == identity]
            saves = [event.occurred_at_ms for event, _ in matching if event.event_name == "flight_added"]
            details = [event.occurred_at_ms for event, _ in matching if event.event_name == "screen_flight_detail_viewed"]
            activation_counts["missing_save"] += not saves
            activation_counts["missing_detail"] += not details
            activation_counts["correct"] += bool(saves and details and min(saves) <= max(details))
        attempts = defaultdict(list)
        attempt_users = defaultdict(set)
        selected_plans = Counter()
        for row in arm:
            for event, props in events_by_entry[row.id]:
                if event.checkout_attempt_id:
                    attempts[(row.id, event.checkout_attempt_id)].append((event, props))
                if event.event_name == "subscription_product_selected":
                    selected_plans[(props.get("selection_method", "unknown"), props.get("product_id", "unknown"))] += 1
        outcome_counts = Counter()
        for (identity, _), values in attempts.items():
            outcomes = [props.get("outcome", "unknown") for event, props in values if event.event_name == "checkout_attempt_completed"]
            outcome = outcomes[0] if len(outcomes) == 1 else "unresolved" if not outcomes else "conflicting"
            outcome_counts[outcome] += 1
            attempt_users[outcome].add(identity)
        economies = []
        for days in (14, 30):
            horizon = days * DAY_MS
            mature = [row for row in arm if row.enrolled_at_ms + horizon <= cutoff]
            native = defaultdict(lambda: {"gross_milliunits": 0, "refund_milliunits": 0, "transactions": 0, "payer_ids": set(), "trial_to_paid_ids": set()})
            mature_trial_ids = set()
            trial_expiry_quality = Counter()
            for row in mature:
                facts = [fact for fact in facts_by_entry[row.id] if fact.purchase_date_ms <= row.enrolled_at_ms + horizon]
                trial = _first(facts, _is_trial)
                trial_mature = bool(trial and trial.expires_date_ms is not None and trial.expires_date_ms <= row.enrolled_at_ms + horizon)
                if trial_mature:
                    mature_trial_ids.add(row.id)
                elif trial:
                    trial_expiry_quality["unknown_expiry" if trial.expires_date_ms is None else "unmatured_at_horizon"] += 1
                for fact in facts:
                    if not _is_paid(fact):
                        continue
                    value = native[(fact.currency, fact.product_id)]
                    value["transactions"] += 1
                    value["payer_ids"].add(row.id)
                    value["gross_milliunits"] += fact.price_milliunits
                    if fact.revoked_date_ms is not None and fact.revoked_date_ms <= min(cutoff, row.enrolled_at_ms + horizon):
                        value["refund_milliunits"] += refunded_milliunits(fact.price_milliunits, fact.revocation_percentage)
                    if trial_mature and trial.purchase_date_ms < fact.purchase_date_ms:
                        value["trial_to_paid_ids"].add(row.id)
            economies.append({"horizon_days": days, "mature_installations": len(mature), "censored_installations": len(arm) - len(mature),
                "trial_maturity": {"verified_expired_trials": len(mature_trial_ids), **dict(trial_expiry_quality)},
                "native_currency_actual_products": [{"currency": currency, "product_id": product, "paid_transactions": value["transactions"],
                    "gross_milliunits": value["gross_milliunits"], "refund_milliunits": value["refund_milliunits"],
                    "refund_adjusted_gross_milliunits": value["gross_milliunits"] - value["refund_milliunits"],
                    "refund_adjusted_gross_per_installation_milliunits": (value["gross_milliunits"] - value["refund_milliunits"]) / len(mature) if mature else None,
                    "payer_conversion": _rate(len(value["payer_ids"]), len(mature)),
                    "trial_to_paid": _rate(len(value["trial_to_paid_ids"]), len(mature_trial_ids))}
                    for (currency, product), value in sorted(native.items())],
                "revenue_label": "Customer payments minus observed refunds, not developer proceeds. Currency/product rows may share installations; do not sum their conversion counts."})
        arms.append({"variant": variant, "enrolled_installations": len(arm),
            "first_valid_selection_10m": _rate(sum(row.id in selections and row.enrolled_at_ms <= selections[row.id].selected_at_ms <= row.enrolled_at_ms + 600_000 for row in mature10), len(mature10)),
            "search_reach_10m": _rate(sum(event_within(row, "af_search", 600_000) for row in mature10), len(mature10)),
            "successful_discovery_10m": _rate(sum(event_within(row, "search_completed", 600_000, lambda props: props.get("has_results") is True) for row in mature10), len(mature10)),
            "paywall_reach_24h": _rate(sum(event_within(row, "paywall_viewed", DAY_MS) for row in mature24), len(mature24)),
            "paywall_surfaces_24h": {surface: _rate(sum(event_within(row, "paywall_viewed", DAY_MS, lambda props: props.get("paywall_surface") == surface) for row in mature24), len(mature24)) for surface in ("selected_flight", "skip_flight", "other")},
            "verified_trial_24h": _rate(sum(any(_is_trial(fact) and fact.purchase_date_ms <= row.enrolled_at_ms + DAY_MS for fact in facts_by_entry[row.id]) for row in mature24), len(mature24)),
            "verified_payer_24h": _rate(sum(any(_is_paid(fact) and fact.purchase_date_ms <= row.enrolled_at_ms + DAY_MS for fact in facts_by_entry[row.id]) for row in mature24), len(mature24)),
            "selected_flight_verified_trial_24h": _rate(sum(any(_is_trial(fact) and selections[row.id].selected_at_ms <= fact.purchase_date_ms <= selections[row.id].selected_at_ms + DAY_MS for fact in facts_by_entry[row.id]) for row in conditional), len(conditional)),
            "correct_post_purchase_activation_24h": {**_rate(activation_counts["correct"], len(purchased_selected)), "missing_save": activation_counts["missing_save"], "missing_detail": activation_counts["missing_detail"],
                "missing_checkout_attribution": activation_counts["missing_checkout_attribution"], "conflicting_checkout_attribution": activation_counts["conflicting_checkout_attribution"],
                "not_selected_flight_purchase": activation_counts["not_selected_flight_purchase"],
                "proof": "Verified transaction joined to its frozen checkout attempt/presentation/product/flight identity, then matching client save and subsequent detail. Missing checkout joins are unavailable, not failed activation. First discovery identity is not substituted."},
            "censored": {"ten_minutes": len(arm) - len(mature10), "twenty_four_hours": len(arm) - len(mature24)},
            "checkout": {"unique_installations": len({identity for identity, _ in attempts}), "attempts": len(attempts), "outcome_attempts": dict(outcome_counts), "outcome_unique_installations": {key: len(value) for key, value in attempt_users.items()}, "plan_selection_diagnostic_counts": [{"selection_method": key[0], "actual_product_id": key[1], "events": count} for key, count in sorted(selected_plans.items())]},
            "goals": {"not_asked": sum(JOURNEY_SCOPES[row.variant][2] == "not_asked" for row in arm), "confirmed_installations": sum(row.id in goal_rows for row in arm),
                "final_revision_after_cutoff_unknown": sum(row.id in later_goal_ids for row in arm),
                "as_of_semantics": "Current final confirmations accepted/captured by cutoff only. A later final revision makes that installation's earlier answer unavailable; it is not substituted into the past or treated as an empty answer.",
                "final_choice_installations": dict(Counter(key for row in arm if row.id in goal_rows for key in goal_rows[row.id].selected_goal_keys.split(",")))},
            "mature_economics": economies,
            "original_entry_strata": [{"app_version": key[0], "build_number": key[1], "config_version": key[2], "installations": count} for key, count in sorted(Counter((row.app_version, row.build_number, row.config_version) for row in arm).items())],
        })
        old10 = sum(row.enrolled_at_ms < now - 90 * DAY_MS for row in mature10)
        old24 = sum(row.enrolled_at_ms < now - 90 * DAY_MS for row in mature24)
        def unavailable(metric, affected):
            return {"numerator": None, "denominator": metric["denominator"], "rate": None, "wilson_95": None,
                    "status": "unavailable_diagnostic_retention_gap", "installations_with_unproven_retention": affected}
        for key in ("search_reach_10m", "successful_discovery_10m"):
            if old10:
                arms[-1][key] = unavailable(arms[-1][key], old10)
        if old24:
            arms[-1]["paywall_reach_24h"] = unavailable(arms[-1]["paywall_reach_24h"], old24)
            arms[-1]["paywall_surfaces_24h"] = {surface: unavailable(metric, old24) for surface, metric in arms[-1]["paywall_surfaces_24h"].items()}
        old_purchasers = sum(row.enrolled_at_ms < now - 90 * DAY_MS and first_activation(row) is not None for row in arm)
        if old_purchasers:
            metric = arms[-1]["correct_post_purchase_activation_24h"]
            metric.update(numerator=None, denominator=None, rate=None, wilson_95=None,
                          status="unavailable_diagnostic_retention_gap", installations_with_unproven_retention=old_purchasers)
        arms[-1]["checkout"]["coverage"] = "retained_diagnostics_only_not_complete" if any(row.enrolled_at_ms < now - 90 * DAY_MS for row in arm) else "retained_window_covered_client_delivery_unproven"
    # Current ledger completeness is stated separately from any client delivery
    # assumption. Missing transactions have unknown ownership, never organic.
    unjoined = session.exec(select(func.count()).select_from(ActivationJourneyDiagnosticContext).join(
        ExperimentDiagnosticEvent, ExperimentDiagnosticEvent.id == ActivationJourneyDiagnosticContext.id,
    ).outerjoin(ActivationJourneyEnrollment, ActivationJourneyEnrollment.id == ActivationJourneyDiagnosticContext.enrollment_id).where(
        ActivationJourneyEnrollment.id.is_(None), ExperimentDiagnosticEvent.analytics_environment == analytics_environment,
        ExperimentDiagnosticEvent.occurred_at_ms <= cutoff,
    )).one()
    effective = Counter((props.get("effective_onboarding", "unknown"), props.get("effective_paywall", "unknown"), props.get("effective_offer", "unknown"), props.get("operational_override", "unknown")) for values in events_by_entry.values() for _, props in values)
    result.update(arms=arms, quality={"unjoined_diagnostic_events_environment_wide": unjoined,
        "mismatched_context_events_excluded": mismatched_context, "mismatched_financial_facts_excluded": mismatched_financial,
        "nonrandomized_entries_excluded": [{"assignment_source": key[0], "variant": key[1], "installations": value} for key, value in sorted(fallback_counts.items())],
        "effective_delivery_diagnostic_strata": [{"effective_onboarding": key[0], "effective_paywall": key[1], "effective_offer": key[2], "operational_override": key[3], "events": count} for key, count in sorted(effective.items())],
        "acquisition_strata": "unavailable_not_inferred", "unattributed_verified_transactions": "Unknown attribution; not allocated to an arm or classified as organic."})
    comparable = ("first_valid_selection_10m", "verified_trial_24h", "verified_payer_24h")
    result["absolute_percentage_point_differences_search_first_minus_goals"] = {
        key: (arms[0][key]["rate"] - arms[1][key]["rate"]) * 100 if arms[0][key]["rate"] is not None and arms[1][key]["rate"] is not None else None for key in comparable}
    # Keep the existing comparison's identities unchanged; the combined
    # candidate is a separate row/difference, never a replacement goals arm.
    result["absolute_percentage_point_differences_search_first_flight_detail_minus_search_first_standard"] = {
        key: (arms[2][key]["rate"] - arms[0][key]["rate"]) * 100 if arms[2][key]["rate"] is not None and arms[0][key]["rate"] is not None else None for key in comparable}
    result["percentage_point_difference_scope"] = "Descriptive differences within the requested extraction. Require compatible concurrent entry/build/configuration windows before causal interpretation; do not pool the old goals arm with the new candidate."
    return result


def baseline_manifest(*, session, analytics_environment, as_of_ms, after=None, limit=500):
    """Keyset export of candidate *source records*, never an approved baseline.

    A single installation can have multiple source records. Consumers must retain
    the installation deduplication key and exhaust next_cursor before combining.
    """
    if as_of_ms < 0 or as_of_ms > current_time_ms() + 300_000:
        raise HTTPException(status_code=422, detail="Invalid baseline extraction cutoff")
    exposure = select((literal("exposure:") + ExperimentExposure.id).label("cursor"),
        literal("exposure").label("source_table"), ExperimentExposure.id.label("record_id"),
        ExperimentExposure.installation_id, ExperimentExposure.experiment_id, ExperimentExposure.variant,
        ExperimentExposure.app_version, ExperimentExposure.build_number, ExperimentExposure.analytics_environment,
        ExperimentExposure.source.label("provenance"), ExperimentExposure.exposed_at_ms.label("entry_at_ms"),
        literal(None).label("measurement_revision"), ExperimentExposure.first_reported_at_ms,
        ExperimentExposure.eligible, literal(None).label("effective_variant"), literal(None).label("config_version")).where(
        ExperimentExposure.analytics_environment == analytics_environment,
        ExperimentExposure.first_reported_at_ms <= as_of_ms,
        ExperimentExposure.exposed_at_ms <= as_of_ms,
        ExperimentExposure.experiment_id.in_(("activation_experience_2026_08", "paywall_flight_detail_2026_09", "paywall_annual_first_2026_09")))
    enrollment = select((literal("enrollment:") + ExperimentEnrollment.id).label("cursor"),
        literal("enrollment").label("source_table"), ExperimentEnrollment.id.label("record_id"),
        ExperimentEnrollment.installation_id, ExperimentEnrollment.experiment_id, ExperimentEnrollment.variant,
        ExperimentEnrollment.app_version, ExperimentEnrollment.build_number, ExperimentEnrollment.analytics_environment,
        ExperimentEnrollment.assignment_source.label("provenance"), ExperimentEnrollment.enrolled_at_ms.label("entry_at_ms"),
        ExperimentEnrollment.measurement_revision, ExperimentEnrollment.first_reported_at_ms,
        ExperimentEnrollment.eligible, ExperimentEnrollment.effective_variant, ExperimentEnrollment.config_version).where(
        ExperimentEnrollment.analytics_environment == analytics_environment,
        ExperimentEnrollment.first_reported_at_ms <= as_of_ms, ExperimentEnrollment.enrolled_at_ms <= as_of_ms,
        ExperimentEnrollment.experiment_id == "paywall_flight_detail_2026_09")
    combined = union_all(exposure, enrollment).subquery()
    total = session.exec(select(func.count()).select_from(combined)).one()
    unique_installations = session.exec(select(func.count(func.distinct(combined.c.installation_id)))).one()
    query = select(combined).order_by(combined.c.cursor)
    if after is not None:
        query = query.where(combined.c.cursor > after)
    page = session.execute(query.limit(limit + 1)).mappings().all()
    records = []
    for item in page[:limit]:
        row = dict(item)
        old_control = row["experiment_id"] == "activation_experience_2026_08" and row["variant"] != "treatment_simplified"
        row.update(installation_deduplication_key=row["installation_id"],
                   baseline_membership="excluded_old_onboarding" if old_control else "unclassified_requires_delivery_audit",
                   experience_candidate="goals_standard" if (row["experiment_id"], row["variant"]) in (("activation_experience_2026_08", "treatment_simplified"), ("paywall_flight_detail_2026_09", "control_current_paywall")) else None,
                   entry_milestone="selected_flight" if row["source_table"] == "enrollment" else "recorded_exposure_not_necessarily_welcome",
                   delivered_layout_offer_evidence="unavailable_in_ledger_requires_diagnostic_configuration_join",
                   all_entrant_rate="unavailable_until_trustworthy_assignment_at_entry_and_coverage_proven",
                   selection_10m_rate="unavailable_not_inferred_from_later_exposure")
        records.append(row)
    return {"as_of_ms": as_of_ms, "extracted_at_ms": current_time_ms(), "analytics_environment": analytics_environment,
            "total_source_records_at_cutoff": total, "unique_installations_at_cutoff": unique_installations,
            "count": len(records), "has_more": len(page) > limit,
            "next_cursor": records[-1]["cursor"] if len(page) > limit else None,
            "records": records, "baseline_ready": False,
            "coverage": "Complete source-record extraction requires all pages at the same cutoff. Source ledgers are mutable and diagnostics have 90-day retention; this is not proof of complete historical delivery or an immutable database snapshot.",
            "combination_rule": "Do not sum overlapping installation records or pool different entry milestones/revisions. Preserve constituent periods and audit annual-first participation, offer/layout/preview, acquisition and operational overrides."}
