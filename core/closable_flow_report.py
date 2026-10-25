"""Read-only closable-flow test metrics. Aggregates only, never identifiers.

Stdlib only: scripts/report_closable_flow_test.py loads this file directly,
without the `core` package initializer (it opens and writes the application
database). Extraction uses plain SQL through a `query(sql, params)` callable
returning dictionaries, so the API route and the read-only SQLite script share
one implementation.

Denominator: randomized, eligible `server_assignment` enrollments of a single
measurement revision and environment. Revisions are never pooled.
"""

import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

try:
    from . import closable_flow_policy as policy
except ImportError:  # Loaded as a top-level module by the read-only script.
    import closable_flow_policy as policy

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
ENTRY_CAP = 20_000
FACT_CAP = 100_000
DIAGNOSTIC_RETENTION_MS = 90 * DAY_MS
FUNNEL_HORIZON_DAYS = 35
FUNNEL_SETTLE_DAYS = 2
# Owner decision 2026-10-21: decide on committed payers per install. The
# horizon is pre-registered here; D1 is reported alongside for an early look.
PRIMARY_HORIZON_DAYS = 7
EARLY_HORIZONS_DAYS = (1, 7)
CONFIRMATION_HORIZONS_DAYS = (14, 35)
RETURN_WINDOW_MS = 7 * DAY_MS
DECISION_THRESHOLD = 0.80
PURCHASE_ENVIRONMENTS = {
    "production": ("Production",), "testflight": ("Sandbox",), "development": ("Sandbox", "Xcode"),
}
TERMINAL_OUTCOMES = ("verified", "cancelled", "pending", "unverified", "error")
COMMITTED_COMPONENTS = ("kept_annual_trial", "weekly_purchase", "lifetime_purchase", "annual_purchase")


# --- Funnel facts (derived daily into the non-purged ledger) ----------------

def derive_funnel_fact(events, *, enrolled_at_ms, horizon_ms, through_ms):
    """Summarize one enrollment's retained diagnostics; counts and times only.

    `events` are dictionaries with event_name, occurred_at_ms,
    paywall_presentation_id, checkout_attempt_id and a properties dictionary.
    """
    end = min(enrolled_at_ms + horizon_ms, through_ms)
    window = sorted((event for event in events if enrolled_at_ms <= event["occurred_at_ms"] <= end),
                    key=lambda event: (event["occurred_at_ms"], event["event_name"]))
    presentations = {}
    for event in window:
        key = event.get("paywall_presentation_id")
        if event["event_name"] == "paywall_viewed" and key and key not in presentations:
            properties = event["properties"]
            presentations[key] = {
                "at": event["occurred_at_ms"], "source": properties.get("source") or "unknown",
                "paywall": properties.get("effective_paywall"), "offer": properties.get("effective_offer"),
                "override": properties.get("operational_override"),
            }
    ordered = sorted(presentations, key=lambda key: (presentations[key]["at"], key))
    first_presentation = ordered[0] if ordered else None
    closes = [event for event in window if event["event_name"] == "paywall_dismissed"
              and event["properties"].get("source") == "close"]
    first_close = min((event["occurred_at_ms"] for event in closes), default=None)
    returns = [key for key in ordered if first_close is not None and presentations[key]["at"] > first_close]

    attempts = defaultdict(list)
    for event in window:
        if event.get("checkout_attempt_id"):
            attempts[event["checkout_attempt_id"]].append(event)
    outcomes = Counter()
    verified = []
    for rows in attempts.values():
        terminals = [row["properties"].get("outcome") or "unknown" for row in rows
                     if row["event_name"] == "checkout_attempt_completed"]
        outcome = terminals[0] if len(terminals) == 1 else "unresolved" if not terminals else "conflicting"
        outcomes[outcome] += 1
        if outcome == "verified":
            presentation = next((row["paywall_presentation_id"] for row in rows if row.get("paywall_presentation_id")), None)
            starts = [row["occurred_at_ms"] for row in rows if row["event_name"] == "af_initiated_checkout"]
            verified.append((presentation, min(starts or [row["occurred_at_ms"] for row in rows])))

    paywall_actions, card_actions = Counter(), Counter()
    retry_taps = defaultdict(list)
    for event in window:
        if event["event_name"] != "experience_action":
            continue
        screen, action = event["properties"].get("screen"), event["properties"].get("action")
        if screen == "paywall":
            paywall_actions[action] += 1
            if action == "retry_checkout_tapped" and event.get("paywall_presentation_id"):
                retry_taps[event["paywall_presentation_id"]].append(event["occurred_at_ms"])
        elif screen == "pending_flight":
            card_actions[action] += 1
    reminder_presentations = {key for key in ordered if presentations[key]["source"] in policy.DEPARTURE_REMINDER_SOURCES}
    return_set = set(returns)
    return {
        "source_event_count": len(window),
        "first_paywall_at_ms": presentations[first_presentation]["at"] if first_presentation else None,
        "paywall_presentations": len(ordered),
        "presentations_by_source_json": json.dumps(dict(sorted(Counter(presentations[key]["source"] for key in ordered).items())),
                                                   separators=(",", ":")),
        "closable_presentations": sum(presentations[key]["paywall"] in ("closable_one_screen", "soft_one_screen") for key in ordered),
        "lifetime_offer_presentations": sum(presentations[key]["offer"] == policy.ANNUAL_WEEKLY_LIFETIME
                                            and presentations[key]["override"] != "lifetime_disabled" for key in ordered),
        "forced_control_presentations": sum(presentations[key]["override"] == "forced_control" for key in ordered),
        "lifetime_disabled_presentations": sum(presentations[key]["override"] == "lifetime_disabled" for key in ordered),
        "close_presentations": len({event.get("paywall_presentation_id") for event in closes if event.get("paywall_presentation_id")}),
        "first_close_at_ms": first_close,
        "return_presentations": len(returns),
        "first_return_at_ms": presentations[returns[0]]["at"] if returns else None,
        "first_return_source": presentations[returns[0]]["source"] if returns else None,
        "checkout_attempts": len(attempts),
        "terminal_outcomes_json": json.dumps(dict(sorted(outcomes.items())), separators=(",", ":")),
        "verified_first_presentation_attempts": sum(presentation is not None and presentation == first_presentation
                                                    for presentation, _ in verified),
        "verified_return_presentation_attempts": sum(presentation in return_set for presentation, _ in verified),
        "retry_panel_shown": paywall_actions["retry_panel_shown"],
        "retry_checkout_tapped": paywall_actions["retry_checkout_tapped"],
        "retry_panel_closed": paywall_actions["retry_panel_closed"],
        "retry_verified_attempts": sum(presentation in retry_taps and started >= min(retry_taps[presentation])
                                       for presentation, started in verified),
        "reminder_offer_shown": card_actions["reminder_offer_shown"],
        "reminder_opted_in": card_actions["reminder_opted_in"],
        "reminder_declined": card_actions["reminder_declined"],
        "reminder_turned_off": card_actions["reminder_turned_off"],
        "reminder_push_opened": sum(event["event_name"] == "push_opened"
                                    and event["properties"].get("notification_type") == policy.DEPARTURE_REMINDER_NOTIFICATION
                                    for event in window),
        "reminder_paywall_presentations": len(reminder_presentations),
        "reminder_verified_attempts": sum(presentation in reminder_presentations for presentation, _ in verified),
        "onboarding_rating_prompts": sum(event["event_name"] == "review_prompt_requested"
                                         and event["properties"].get("source") == policy.ONBOARDING_RATING_SOURCE
                                         for event in window),
    }


# --- Verified Apple facts ----------------------------------------------------

def refunded_milliunits(price_milliunits, revocation_percentage):
    """Same rule as core.services.revenue_measurement: milli-percent, half-up."""
    percentage = 100_000 if revocation_percentage is None else min(100_000, max(0, revocation_percentage))
    return (max(0, price_milliunits) * percentage + 50_000) // 100_000


def is_trial(fact):
    return bool(fact["starts_trial"]) and fact["price_milliunits"] == 0


def is_paid(fact):
    return not fact["starts_trial"] and fact["price_milliunits"] > 0


def _refund_at(fact, cutoff):
    if fact["revoked_date_ms"] is None or fact["revoked_date_ms"] > cutoff:
        return 0
    return refunded_milliunits(fact["price_milliunits"], fact["revocation_percentage"])


def _fully_refunded(fact, cutoff):
    return fact["revoked_date_ms"] is not None and fact["revoked_date_ms"] <= cutoff and (
        _refund_at(fact, cutoff) >= max(1, fact["price_milliunits"]))


def _lifecycle_between(rows, start, end):
    return sorted((row for row in rows if row["notification_signed_date_ms"] is not None
                   and start <= row["notification_signed_date_ms"] <= end),
                  key=lambda row: (row["notification_signed_date_ms"], row["id"]))


def auto_renew_on_at(fact, lifecycle, moment):
    """Auto-renew state at `moment`; on unless a later change turned it off."""
    state = True
    for row in _lifecycle_between(lifecycle, fact["purchase_date_ms"], moment):
        if row["event_kind"] == "auto_renew_disabled":
            state = False
        elif row["event_kind"] == "auto_renew_enabled":
            state = True
        elif row["event_kind"] == "auto_renew_status_changed" and row["auto_renew_status"] in (0, 1):
            state = bool(row["auto_renew_status"])
    return state


def standing_at(fact, lifecycle, moment):
    if fact["revoked_date_ms"] is not None and fact["revoked_date_ms"] <= moment:
        return False
    return not any(row["event_kind"] in ("refund", "revocation")
                   for row in _lifecycle_between(lifecycle, fact["purchase_date_ms"], moment))


def committed_component(fact, lifecycle):
    """Owner decision 2026-10-21: why a verified fact makes a committed payer.

    Checked 24 h after the purchase: an annual trial with auto-renew still on
    and not refunded, or a weekly/Lifetime/annual payment not refunded.
    """
    moment = fact["purchase_date_ms"] + DAY_MS
    product = fact["product_id"]
    if is_trial(fact) and product not in ({policy.WEEKLY_PRODUCT_ID} | policy.LIFETIME_PRODUCT_IDS):
        if standing_at(fact, lifecycle, moment) and auto_renew_on_at(fact, lifecycle, moment):
            return "kept_annual_trial"
        return None
    if not is_paid(fact) or not standing_at(fact, lifecycle, moment):
        return None
    if product == policy.WEEKLY_PRODUCT_ID:
        return "weekly_purchase"
    if product in policy.LIFETIME_PRODUCT_IDS:
        return "lifetime_purchase"
    return "annual_purchase"


def _is_immediate(fact):
    # The first payment of a purchase, not a renewal or a trial conversion.
    return is_paid(fact) and fact["transaction_reason"] != "RENEWAL"


# --- Statistics ---------------------------------------------------------------

def _rate(numerator, denominator):
    if not denominator:
        return {"numerator": numerator, "denominator": denominator, "rate": None, "wilson_95": None}
    p = numerator / denominator
    z = 1.959963984540054
    center = (p + z * z / (2 * denominator)) / (1 + z * z / denominator)
    half = z * math.sqrt(p * (1 - p) / denominator + z * z / (4 * denominator * denominator)) / (1 + z * z / denominator)
    return {"numerator": numerator, "denominator": denominator, "rate": p,
            "wilson_95": [max(0.0, center - half), min(1.0, center + half)]}


def _log_beta(a, b):
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def probability_test_beats_control(test_successes, test_trials, control_successes, control_trials):
    """Exact P(p_test > p_control) under independent Beta(1, 1) priors.

    Closed form for integer Beta parameters (sum over the test arm's alpha).
    """
    if test_trials <= 0 or control_trials <= 0:
        return None
    alpha_t, beta_t = 1 + test_successes, 1 + test_trials - test_successes
    alpha_c, beta_c = 1 + control_successes, 1 + control_trials - control_successes
    total = 0.0
    for i in range(alpha_t):
        total += math.exp(_log_beta(alpha_c + i, beta_c + beta_t) - math.log(beta_t + i)
                          - _log_beta(1 + i, beta_t) - _log_beta(alpha_c, beta_c))
    return min(1.0, max(0.0, total))


# --- Extraction -----------------------------------------------------------------

def _cohort(experiment_id, analytics_environment, revision, cutoff, since_ms, until_ms, app_version, build_number):
    clauses = ["experiment_id = ?", "analytics_environment = ?", "enrolled_at_ms <= ?", "first_reported_at_ms <= ?"]
    params = [experiment_id, analytics_environment, cutoff, cutoff]
    for clause, value in (("measurement_revision = ?", revision), ("enrolled_at_ms >= ?", since_ms), ("enrolled_at_ms < ?", until_ms),
                          ("app_version = ?", app_version), ("build_number = ?", build_number)):
        if value is not None:
            clauses.append(clause)
            params.append(value)
    return " AND ".join(clauses), params


def extract(query, *, analytics_environment, revision, cutoff, since_ms=None, until_ms=None,
            app_version=None, build_number=None):
    where, params = _cohort(policy.EXPERIMENT_ID, analytics_environment, revision, cutoff,
                            since_ms, until_ms, app_version, build_number)
    any_revision, any_revision_params = _cohort(policy.EXPERIMENT_ID, analytics_environment, None, cutoff,
                                                since_ms, until_ms, app_version, build_number)
    cohort_ids = f"SELECT id FROM activationjourneyenrollment WHERE {where}"
    attributed = f"SELECT id FROM activationjourneyattribution WHERE enrollment_id IN ({cohort_ids}) AND attributed_at_ms <= ?"
    environments = PURCHASE_ENVIRONMENTS[analytics_environment]
    marks = ", ".join("?" for _ in environments)
    tables = {row["name"] for row in query("SELECT name FROM sqlite_master WHERE type = 'table'", ())}
    data = {
        "entries": query(
            "SELECT id, user_id, variant, eligible, randomized, assignment_source, enrolled_at_ms, app_version, "
            f"build_number, config_version, context_json FROM activationjourneyenrollment WHERE {where} "
            "ORDER BY id LIMIT ?", (*params, ENTRY_CAP + 1)),
        "attributions": query(
            "SELECT id, enrollment_id, user_id, context_json FROM activationjourneyattribution "
            f"WHERE enrollment_id IN ({cohort_ids}) AND attributed_at_ms <= ? ORDER BY id LIMIT ?",
            (*params, cutoff, FACT_CAP + 1)),
        "revenue": query(
            "SELECT id, original_transaction_id, product_id, purchase_date_ms, revoked_date_ms, revocation_percentage, "
            "transaction_reason, purchase_environment, price_milliunits, currency, starts_trial FROM appstorerevenueevent "
            f"WHERE original_transaction_id IN ({attributed}) AND purchase_date_ms <= ? ORDER BY id LIMIT ?",
            (*params, cutoff, cutoff, FACT_CAP + 1)),
        "lifecycle": query(
            "SELECT id, original_transaction_id, event_kind, auto_renew_status, notification_signed_date_ms "
            f"FROM appstoresubscriptionlifecycleevent WHERE original_transaction_id IN ({attributed}) "
            f"AND purchase_environment IN ({marks}) AND notification_signed_date_ms <= ? ORDER BY id LIMIT ?",
            (*params, cutoff, *environments, cutoff, FACT_CAP + 1)),
        "assignments": query(
            "SELECT id, context_json, created_at_ms FROM activationjourneyassignment "
            "WHERE substr(id, 1, ?) = ? AND created_at_ms <= ? ORDER BY id LIMIT ?",
            (len(policy.EXPERIMENT_ID) + 1, policy.EXPERIMENT_ID + ":", cutoff, ENTRY_CAP + 1)),
        "pre_enrollment_purchases": query(
            "SELECT DISTINCT e.id AS enrollment_id FROM activationjourneyenrollment e "
            "JOIN usersubscriptionlink l ON l.user_id = e.user_id "
            "JOIN appstorerevenueevent r ON r.original_transaction_id = l.subscription_id "
            f"WHERE e.id IN ({cohort_ids}) AND r.purchase_environment IN ({marks}) AND r.purchase_date_ms <= ? "
            "AND COALESCE(r.original_purchase_date_ms, r.purchase_date_ms) < e.enrolled_at_ms",
            (*params, *environments, cutoff)),
        # Client fallbacks cannot know the server's revision (the app labels them
        # revision 1), so daily assignment health spans revisions.
        "client_fallbacks": query(
            f"SELECT id, assignment_source, enrolled_at_ms FROM activationjourneyenrollment WHERE {any_revision} "
            "AND assignment_source IN ('configuration_fallback', 'local_debug_preview') ORDER BY id LIMIT ?",
            (*any_revision_params, ENTRY_CAP + 1)),
        "funnel_table_present": "closableflowfunnelfact" in tables,
        "funnel_facts": query(
            f"SELECT * FROM closableflowfunnelfact WHERE id IN ({cohort_ids}) ORDER BY id LIMIT ?",
            (*params, ENTRY_CAP + 1)) if "closableflowfunnelfact" in tables else [],
    }
    # Enrollments without a ledger row (daily job not run yet) are derived in
    # memory from retained diagnostics; nothing is written.
    missing = " AND c.enrollment_id NOT IN (SELECT id FROM closableflowfunnelfact)" if "closableflowfunnelfact" in tables else ""
    data["live_events"] = query(
        "SELECT e.event_name, e.occurred_at_ms, e.paywall_presentation_id, e.checkout_attempt_id, e.properties_json, "
        "c.enrollment_id, c.user_id, c.context_json FROM experimentdiagnosticevent e "
        "JOIN activationjourneydiagnosticcontext c ON c.id = e.id "
        f"WHERE c.enrollment_id IN ({cohort_ids}){missing} AND e.occurred_at_ms <= ? AND e.received_at_ms <= ? "
        "ORDER BY e.id LIMIT ?", (*params, cutoff, cutoff, FACT_CAP + 1))
    # unknown_protocol (M7): the app could not save any context, so its
    # schema-28 onboarding starts carry neither a journey nor a legacy cohort.
    # Revision-less and retained only 90 days; a daily quality count.
    clauses, values = [], []
    for clause, value in (("occurred_at_ms >= ?", since_ms), ("occurred_at_ms < ?", until_ms),
                          ("app_version = ?", app_version), ("build_number = ?", build_number)):
        if value is not None:
            clauses.append(" AND " + clause)
            values.append(value)
    data["cohortless_onboarding_starts"] = query(
        "SELECT installation_id, occurred_at_ms FROM experimentdiagnosticevent WHERE event_name = 'onboarding_started' "
        "AND analytics_environment = ? AND experiment_id IS NULL AND occurred_at_ms <= ? AND received_at_ms <= ? "
        "AND json_extract(properties_json, '$._activation_journey') IS NULL "
        f"AND COALESCE(json_extract(properties_json, '$.event_schema_version'), 0) >= ?{''.join(clauses)} "
        "ORDER BY id LIMIT ?",
        (analytics_environment, cutoff, cutoff, policy.MIN_EVENT_SCHEMA, *values, FACT_CAP + 1))
    data["complete"] = (len(data["entries"]) <= ENTRY_CAP and len(data["funnel_facts"]) <= ENTRY_CAP
                        and len(data["assignments"]) <= ENTRY_CAP and len(data["client_fallbacks"]) <= ENTRY_CAP
                        and all(len(data[key]) <= FACT_CAP for key in ("attributions", "revenue", "lifecycle", "live_events",
                                                                      "cohortless_onboarding_starts")))
    return data


def _live_funnel_facts(data, cutoff, now):
    by_id = {row["id"]: row for row in data["entries"]}
    events = defaultdict(list)
    for row in data["live_events"]:
        entry = by_id.get(row["enrollment_id"])
        if entry is None or row["user_id"] != entry["user_id"] or row["context_json"] != entry["context_json"]:
            continue
        events[entry["id"]].append({"event_name": row["event_name"], "occurred_at_ms": row["occurred_at_ms"],
                                    "paywall_presentation_id": row["paywall_presentation_id"],
                                    "checkout_attempt_id": row["checkout_attempt_id"],
                                    "properties": json.loads(row["properties_json"])})
    stored = {row["id"] for row in data["funnel_facts"]}
    facts = {}
    for entry in data["entries"]:
        if entry["id"] in stored:
            continue
        derived = derive_funnel_fact(events[entry["id"]], enrolled_at_ms=entry["enrolled_at_ms"],
                                     horizon_ms=FUNNEL_HORIZON_DAYS * DAY_MS, through_ms=cutoff)
        facts[entry["id"]] = {**derived, "id": entry["id"], "enrolled_at_ms": entry["enrolled_at_ms"],
                              "derived_through_ms": min(cutoff, entry["enrolled_at_ms"] + FUNNEL_HORIZON_DAYS * DAY_MS),
                              "retention_complete": entry["enrolled_at_ms"] >= now - DIAGNOSTIC_RETENTION_MS,
                              "final": False, "derived_at_ms": cutoff, "derived_live": True}
    return facts


# --- Summary -------------------------------------------------------------------

def _day(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _distinct_by_day(rows):
    installs = defaultdict(set)
    for row in rows:
        installs[_day(row["occurred_at_ms"])].add(row["installation_id"])
    return {day: len(values) for day, values in installs.items()}


def usd_estimate(native, usd_rates, proceeds_ratio):
    """Estimated developer proceeds per install in USD; None without a table."""
    if usd_rates is None or not native.get("mature_installs"):
        return None
    missing = sorted(row["currency"] for row in native["by_currency"] if row["currency"] not in usd_rates)
    customer = sum(row["net_milliunits"] / 1000 * usd_rates[row["currency"]]
                   for row in native["by_currency"] if row["currency"] in usd_rates)
    return {"customer_net_usd_per_install": customer / native["mature_installs"],
            "estimated_proceeds_usd_per_install": customer * proceeds_ratio / native["mature_installs"],
            "proceeds_ratio": proceeds_ratio, "unconverted_currencies": missing, "complete": not missing}


def _native(facts_by_entry, installs, cutoff, horizon_ms, predicate):
    totals = defaultdict(lambda: {"gross_milliunits": 0, "refund_milliunits": 0, "transactions": 0, "payers": set()})
    for row in installs:
        for fact in facts_by_entry[row["id"]]:
            if fact["purchase_date_ms"] > row["enrolled_at_ms"] + horizon_ms or not predicate(fact):
                continue
            value = totals[(fact["currency"], fact["product_id"])]
            value["transactions"] += 1
            value["payers"].add(row["id"])
            value["gross_milliunits"] += fact["price_milliunits"]
            value["refund_milliunits"] += _refund_at(fact, cutoff)
    by_currency = defaultdict(lambda: {"gross_milliunits": 0, "refund_milliunits": 0, "transactions": 0})
    products = []
    for (currency, product), value in sorted(totals.items()):
        net = value["gross_milliunits"] - value["refund_milliunits"]
        products.append({"currency": currency, "product_id": product, "transactions": value["transactions"],
                         "installs_paying": len(value["payers"]), "gross_milliunits": value["gross_milliunits"],
                         "refund_milliunits": value["refund_milliunits"], "net_milliunits": net,
                         "net_per_install_milliunits": net / len(installs) if installs else None})
        for key in ("gross_milliunits", "refund_milliunits", "transactions"):
            by_currency[currency][key] += value[key]
    currencies = [{"currency": currency, **value,
                   "net_milliunits": value["gross_milliunits"] - value["refund_milliunits"],
                   "net_per_install_milliunits": (value["gross_milliunits"] - value["refund_milliunits"]) / len(installs) if installs else None}
                  for currency, value in sorted(by_currency.items())]
    return {"mature_installs": len(installs), "by_currency": currencies, "by_product": products,
            "label": "Native-currency customer payments minus refunds observed by the cutoff; not developer proceeds. "
                     "Lifetime is a one-off payment and is never annualized."}


def _paywall_metrics(facts):
    usable = [fact for fact in facts if fact["retention_complete"]]
    reach_base = [fact for fact in usable if fact["derived_through_ms"] >= fact["enrolled_at_ms"] + DAY_MS]
    reaching = [fact for fact in usable if fact["paywall_presentations"] > 0]
    closers = [fact for fact in reaching if fact["close_presentations"] > 0]
    return_base = [fact for fact in closers if fact["first_close_at_ms"] + RETURN_WINDOW_MS <= fact["derived_through_ms"]]
    returned = [fact for fact in return_base if fact["first_return_at_ms"] is not None
                and fact["first_return_at_ms"] <= fact["first_close_at_ms"] + RETURN_WINDOW_MS]
    sources = Counter()
    for fact in usable:
        sources.update(json.loads(fact["presentations_by_source_json"] or "{}"))
    outcomes = Counter()
    for fact in usable:
        outcomes.update(json.loads(fact["terminal_outcomes_json"] or "{}"))
    def installs(predicate, base):
        return sum(1 for fact in base if predicate(fact))
    retry_base = [fact for fact in usable if fact["retry_panel_shown"] > 0]
    reminder_base = [fact for fact in usable if fact["reminder_offer_shown"] > 0]
    return {
        "funnel_facts_used": len(usable),
        "reach_24h": _rate(installs(lambda fact: fact["first_paywall_at_ms"] is not None
                                    and fact["first_paywall_at_ms"] <= fact["enrolled_at_ms"] + DAY_MS, reach_base), len(reach_base)),
        "presentations": sum(fact["paywall_presentations"] for fact in usable),
        "presentations_by_source": dict(sorted(sources.items())),
        "close_rate_per_presentation": _rate(sum(fact["close_presentations"] for fact in usable),
                                             sum(fact["paywall_presentations"] for fact in usable)),
        "close_rate_per_reaching_install": _rate(len(closers), len(reaching)),
        "return_within_7d_after_close": _rate(len(returned), len(return_base)),
        "first_return_sources": dict(sorted(Counter(fact["first_return_source"] for fact in returned).items())),
        "median_hours_to_return": statistics.median((fact["first_return_at_ms"] - fact["first_close_at_ms"]) / HOUR_MS
                                                    for fact in returned) if returned else None,
        "installs_converting_on_return": installs(lambda fact: fact["verified_return_presentation_attempts"] > 0, usable),
        "verified_return_attempts": sum(fact["verified_return_presentation_attempts"] for fact in usable),
        "checkout_attempt_outcomes": dict(sorted(outcomes.items())),
        "retry_funnel_installs": {"panel_shown": len(retry_base),
                                  "retry_tapped": installs(lambda fact: fact["retry_checkout_tapped"] > 0, retry_base),
                                  "verified_after_retry": installs(lambda fact: fact["retry_verified_attempts"] > 0, retry_base)},
        "reminder_funnel_installs": {"offer_shown": len(reminder_base),
                                     "opted_in": installs(lambda fact: fact["reminder_opted_in"] > 0, reminder_base),
                                     "push_opened": installs(lambda fact: fact["reminder_push_opened"] > 0, usable),
                                     "reminder_paywall": installs(lambda fact: fact["reminder_paywall_presentations"] > 0, usable),
                                     "verified": installs(lambda fact: fact["reminder_verified_attempts"] > 0, usable)},
        "lifetime_offer_presentations": sum(fact["lifetime_offer_presentations"] for fact in usable),
        "forced_control_presentations": sum(fact["forced_control_presentations"] for fact in usable),
        "lifetime_disabled_presentations": sum(fact["lifetime_disabled_presentations"] for fact in usable),
    }


def summarize(data, *, analytics_environment, revision, cutoff, now, since_ms=None, until_ms=None,
              app_version=None, build_number=None, usd_rates=None, proceeds_ratio=None):
    control_variant, test_variant = policy.variants(revision)
    result = {
        "experiment_id": policy.EXPERIMENT_ID, "measurement_revision": revision,
        "config_version": policy.config_version(revision), "offer_scope": policy.offer_scope(revision),
        "analytics_environment": analytics_environment, "purchase_environments": list(PURCHASE_ENVIRONMENTS[analytics_environment]),
        "as_of_ms": cutoff, "extracted_at_ms": now, "entry_start_inclusive_ms": since_ms, "entry_end_exclusive_ms": until_ms,
        "app_version": app_version, "build_number": build_number,
        "definitions": {
            "denominator": "Randomized, eligible server_assignment enrollments of this revision and environment, mature at each horizon.",
            "committed_payer": "Install with, within H days of enrollment, a verified attributed annual free trial whose auto-renew is still on and that is not refunded 24 h after it started, or a verified weekly, Lifetime or other annual payment not refunded within 24 h. Mature when enrollment + H + 24 h <= as_of.",
            "verified_trial_start": "First attributed verified free trial (starts_trial and price 0) within H days.",
            "immediate_net_revenue": "Verified first payments (not renewals or trial conversions: weekly, Lifetime, annual without trial) within H days, minus refunds observed by as_of, per native currency.",
            "paid_per_install": "Install with at least one attributed verified payment (price > 0, not a trial) by enrollment + H, not fully refunded by as_of.",
            "paywall_funnel": "From the non-purged ClosableFlowFunnelFact ledger (35-day horizon); enrollments without a ledger row are derived in memory from retained diagnostics.",
            "usd_estimate": "Only with a frozen launch-day FX table (USD per currency unit) and a stated proceeds ratio (default 0.80); reconcile with Apple's proceeds.",
            "attribution": "Verified Apple facts joined through ActivationJourneyAttribution (same original account, purchase after enrollment). Unattributed transactions are unknown, never organic.",
        },
    }
    if not data["complete"]:
        return {**result, "arms": {}, "quality": {"status": "unavailable_extraction_cap_exceeded"}}
    entries = data["entries"]
    by_id = {row["id"]: row for row in entries}
    randomized = [row for row in entries if row["eligible"] and row["randomized"] and row["assignment_source"] == "server_assignment"]
    randomized_ids = {row["id"] for row in randomized}
    attribution = {row["id"]: row for row in data["attributions"]}
    environments = PURCHASE_ENVIRONMENTS[analytics_environment]
    facts_by_entry = defaultdict(list)
    mismatched_financial = 0
    for fact in data["revenue"]:
        association = attribution.get(fact["original_transaction_id"])
        entry = by_id.get(association["enrollment_id"]) if association else None
        if (entry is None or association["context_json"] != entry["context_json"] or association["user_id"] != entry["user_id"]
                or fact["purchase_environment"] not in environments or fact["purchase_date_ms"] < entry["enrolled_at_ms"]):
            mismatched_financial += 1
            continue
        facts_by_entry[entry["id"]].append(fact)
    lifecycle = defaultdict(list)
    for row in data["lifecycle"]:
        lifecycle[row["original_transaction_id"]].append(row)
    funnel = {row["id"]: row for row in data["funnel_facts"]}
    funnel.update(_live_funnel_facts(data, cutoff, now))
    pre_enrollment = {row["enrollment_id"] for row in data["pre_enrollment_purchases"]}

    def mature(arm, horizon_ms):
        return [row for row in arm if row["enrolled_at_ms"] + horizon_ms <= cutoff]

    def in_window(row, fact, horizon_ms):
        return row["enrolled_at_ms"] <= fact["purchase_date_ms"] <= row["enrolled_at_ms"] + horizon_ms

    arms, primary_counts = {}, {}
    overall_lifetime = Counter()
    for variant in policy.variants(revision):
        arm = [row for row in randomized if row["variant"] == variant]
        arm_result = {"installs": len(arm), "verified_trial_starts": {}, "committed_payers": {},
                      "immediate_net_revenue": {}, "lifetime_uptake": {}, "paid_per_install": {}, "net_revenue": {}}
        for days in EARLY_HORIZONS_DAYS:
            horizon = days * DAY_MS
            label = f"d{days}"
            trial_base = mature(arm, horizon)
            arm_result["verified_trial_starts"][label] = _rate(
                sum(any(is_trial(fact) and in_window(row, fact, horizon) for fact in facts_by_entry[row["id"]]) for row in trial_base),
                len(trial_base))
            committed_base = mature(arm, horizon + DAY_MS)
            components, committed = Counter(), 0
            trials = kept = cancelled_1h = covered = 0
            for row in committed_base:
                reasons = set()
                for fact in facts_by_entry[row["id"]]:
                    if not in_window(row, fact, horizon):
                        continue
                    rows = lifecycle[fact["original_transaction_id"]]
                    reason = committed_component(fact, rows)
                    if reason:
                        reasons.add(reason)
                    if is_trial(fact) and fact["product_id"] not in ({policy.WEEKLY_PRODUCT_ID} | policy.LIFETIME_PRODUCT_IDS):
                        trials += 1
                        kept += reason == "kept_annual_trial"
                        cancelled_1h += not auto_renew_on_at(fact, rows, fact["purchase_date_ms"] + HOUR_MS)
                        covered += bool(rows)
                components.update(reasons)
                committed += bool(reasons)
            arm_result["committed_payers"][label] = {
                **_rate(committed, len(committed_base)),
                "installs_by_component": {key: components[key] for key in COMMITTED_COMPONENTS},
                "annual_trials": trials, "kept_trial_share": _rate(kept, trials),
                "trials_auto_renew_off_within_1h": _rate(cancelled_1h, trials),
                "trial_lifecycle_coverage": _rate(covered, trials),
            }
            if days == PRIMARY_HORIZON_DAYS:
                primary_counts[variant] = (committed, len(committed_base))
            immediate_base = mature(arm, horizon)
            native = _native(facts_by_entry, immediate_base, cutoff, horizon, _is_immediate)
            native["usd_estimate"] = usd_estimate(native, usd_rates, proceeds_ratio)
            arm_result["immediate_net_revenue"][label] = native
            lifetime_buyers = sum(any(fact["product_id"] in policy.LIFETIME_PRODUCT_IDS and is_paid(fact) and in_window(row, fact, horizon)
                                      for fact in facts_by_entry[row["id"]]) for row in immediate_base)
            arm_result["lifetime_uptake"][label] = _rate(lifetime_buyers, len(immediate_base))
            if days == PRIMARY_HORIZON_DAYS:
                overall_lifetime.update(buyers=lifetime_buyers, installs=len(immediate_base))
        for days in CONFIRMATION_HORIZONS_DAYS:
            horizon = days * DAY_MS
            label = f"d{days}"
            base = mature(arm, horizon)
            if not base:
                arm_result["paid_per_install"][label] = {"status": "not_mature", "censored_installs": len(arm)}
                arm_result["net_revenue"][label] = {"status": "not_mature", "censored_installs": len(arm)}
                continue
            arm_result["paid_per_install"][label] = {
                **_rate(sum(any(is_paid(fact) and in_window(row, fact, horizon) and not _fully_refunded(fact, cutoff)
                                for fact in facts_by_entry[row["id"]]) for row in base), len(base)),
                "censored_installs": len(arm) - len(base)}
            native = _native(facts_by_entry, base, cutoff, horizon, is_paid)
            native["usd_estimate"] = usd_estimate(native, usd_rates, proceeds_ratio)
            arm_result["net_revenue"][label] = native
        payers = [row for row in arm if any(is_paid(fact) for fact in facts_by_entry[row["id"]])]
        refunds = sum(1 for row in arm for fact in facts_by_entry[row["id"]] if is_paid(fact) and _refund_at(fact, cutoff) > 0)
        arm_result["guardrails"] = {"paying_installs": len(payers), "refunded_payments": refunds,
                                    "refunds_per_paying_install": refunds / len(payers) if payers else None,
                                    "outside_this_report": ["crash_free_users", "support_and_review_complaints", "search_provider_load"]}
        facts = [funnel[row["id"]] for row in arm if row["id"] in funnel]
        arm_result["paywall"] = _paywall_metrics(facts)
        arm_result["paywall"]["coverage"] = {
            "installs_with_ledger_fact": sum(not fact.get("derived_live") for fact in facts),
            "installs_derived_live_from_retained_diagnostics": sum(bool(fact.get("derived_live")) for fact in facts),
            "facts_with_retention_gap": sum(not fact["retention_complete"] for fact in facts),
            "stale_unfinished_ledger_facts": sum(not fact["final"] and not fact.get("derived_live")
                                                 and fact["derived_at_ms"] < cutoff - 36 * HOUR_MS for fact in facts)}
        arm_result["onboarding_rating_prompts"] = sum(fact["onboarding_rating_prompts"] for fact in facts)
        arm_result["installs_with_pre_enrollment_purchase"] = sum(row["id"] in pre_enrollment for row in arm)
        arm_result["entry_strata"] = [{"app_version": key[0], "build_number": key[1], "config_version": key[2], "installs": count}
                                      for key, count in sorted(Counter((row["app_version"], row["build_number"], row["config_version"])
                                                                       for row in arm).items())]
        arms[variant] = arm_result

    (x_t, n_t), (x_c, n_c) = primary_counts[test_variant], primary_counts[control_variant]
    probability = probability_test_beats_control(x_t, n_t, x_c, n_c)
    early = {}
    for days in EARLY_HORIZONS_DAYS:
        test, control = arms[test_variant]["committed_payers"][f"d{days}"], arms[control_variant]["committed_payers"][f"d{days}"]
        early[f"d{days}"] = probability_test_beats_control(test["numerator"], test["denominator"],
                                                           control["numerator"], control["denominator"])
    result["decision"] = {
        "primary_metric": f"committed_payers_d{PRIMARY_HORIZON_DAYS}",
        "prior": "independent Beta(1, 1) per arm (exact)",
        "p_test_beats_control": probability,
        "p_control_beats_test": None if probability is None else 1 - probability,
        "p_test_beats_control_by_horizon": early,
        "threshold": DECISION_THRESHOLD,
        "test_meets_threshold": probability is not None and probability >= DECISION_THRESHOLD,
        "control_meets_threshold": probability is not None and 1 - probability >= DECISION_THRESHOLD,
        "rule": "Decide after 3 weeks (2 weeks only for a clear win or a guardrail breach). Switch everyone to the arm with P >= 0.80 on the primary metric only if no guardrail is breached; crash-free users, complaints and search-provider load are checked outside this report. Keep reporting D14/D35 paid per install afterwards; a confirmed loss rolls back remotely.",
    }
    result["overall"] = {f"lifetime_uptake_d{PRIMARY_HORIZON_DAYS}": _rate(overall_lifetime["buyers"], overall_lifetime["installs"]),
                         "lifetime_offered_in_revision": policy.offers_lifetime(revision)}
    result["arms"] = arms

    nonrandomized = Counter((_day(row["enrolled_at_ms"]), row["assignment_source"]) for row in data["client_fallbacks"])
    seen = {row["id"] for row in data["client_fallbacks"]}
    unenrolled = Counter()
    for row in data["assignments"]:
        context = json.loads(row["context_json"])
        if (context.get("analytics_environment") != analytics_environment
                or (since_ms is not None and context["enrolled_at_ms"] < since_ms)
                or (until_ms is not None and context["enrolled_at_ms"] >= until_ms)
                or (app_version is not None and context.get("app_version") != app_version)
                or (build_number is not None and context.get("build_number") != build_number) or row["id"] in seen):
            continue
        nonrandomized[(_day(context["enrolled_at_ms"]), "assignment:" + context["assignment_source"])] += 1
        if (context.get("measurement_revision") == revision and context["assignment_source"] == "server_assignment"
                and row["id"] not in by_id
                and row["created_at_ms"] + DAY_MS <= cutoff):
            unenrolled[context["variant"]] += 1
    days = defaultdict(Counter)
    for (day, source), count in nonrandomized.items():
        days[day][source] += count
    for day, installs in _distinct_by_day(data["cohortless_onboarding_starts"]).items():
        days[day]["unknown_protocol"] += installs
    daily = []
    for day, counts in sorted(days.items()):
        fallback = counts["configuration_fallback"]
        total = counts["assignment:server_assignment"] + counts["assignment:server_disabled"] + fallback
        daily.append({"day_utc": day, "server_assignments": counts["assignment:server_assignment"],
                      "server_disabled": counts["assignment:server_disabled"], "configuration_fallback": fallback,
                      "local_debug_preview": counts["local_debug_preview"], "fallback_share": _rate(fallback, total),
                      "unknown_protocol_onboarding_installs": counts["unknown_protocol"]})
    result["quality"] = {
        "daily_assignment_sources": daily,
        "daily_assignment_sources_scope": "All revisions of this environment: client fallbacks cannot know the server's revision.",
        "server_assignments_without_enrollment_after_24h": {variant: unenrolled[variant] for variant in policy.variants(revision)},
        "nonrandomized_enrollments_excluded": dict(sorted(Counter(row["assignment_source"] for row in entries
                                                                  if row["id"] not in randomized_ids).items())),
        "mismatched_financial_facts_excluded": mismatched_financial,
        "lifetime_offered_outside_lifetime_revision": 0 if policy.offers_lifetime(revision) else sum(
            arm["paywall"]["lifetime_offer_presentations"] for arm in arms.values()),
        "funnel_fact_table_present": data["funnel_table_present"],
        "unknown_protocol_installs": len({row["installation_id"] for row in data["cohortless_onboarding_starts"]}),
        "unknown_protocol_semantics": "Distinct installations with a schema-28 onboarding_started carrying no journey or "
                                      "legacy cohort: installs whose context could not be saved, plus any upgraded install "
                                      "that restarts onboarding (an upper bound). Diagnostics are retained 90 days.",
        "as_of_semantics": "Purchase-time cutoff with refunds and lifecycle events observed by as_of; funnel facts are their latest daily derivation.",
    }
    return result


def build_summary(query, *, analytics_environment="production", revision, as_of_ms=None, now, since_ms=None,
                  until_ms=None, app_version=None, build_number=None, usd_rates=None, proceeds_ratio=0.80):
    cutoff = now if as_of_ms is None else as_of_ms
    if (revision not in policy.REVISIONS or analytics_environment not in PURCHASE_ENVIRONMENTS or cutoff < 0
            or cutoff > now + 300_000 or (since_ms is not None and until_ms is not None and since_ms >= until_ms)
            or not 0 < proceeds_ratio <= 1 or (usd_rates is not None and any(
                not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate <= 0 for rate in usd_rates.values()))):
        raise ValueError("Invalid closable-flow report window or revision")
    data = extract(query, analytics_environment=analytics_environment, revision=revision, cutoff=cutoff,
                   since_ms=since_ms, until_ms=until_ms, app_version=app_version, build_number=build_number)
    return summarize(data, analytics_environment=analytics_environment, revision=revision, cutoff=cutoff, now=now,
                     since_ms=since_ms, until_ms=until_ms, app_version=app_version, build_number=build_number,
                     usd_rates=usd_rates, proceeds_ratio=proceeds_ratio)
