"""Credential-free contract for the closable own-flight flow test.

Stdlib only. The read-only report loads this file directly, without the `core`
package initializer, which opens the application database. The app compiles the
same revision table; a revision or config version outside it is invalid.
"""

import hashlib

EXPERIMENT_ID = "closable_flow_2026_10"
CONTROL = "control_hard_paywall"
TEST = "closable_own_flight"
# Fixed meanings, never reused for another arm.
VARIANTS = (CONTROL, TEST)
MATCHED_CONTROL = "matched_hard_paywall"
MATCHED_TEST = "matched_soft_paywall"
FALLBACK_VARIANT = CONTROL
MIN_EVENT_SCHEMA = 28
EXPOSURE_SCOPE = "once_per_installation_assignment"

# Tuple order: intended onboarding, intended paywall, goals status.
SCOPES = {
    CONTROL: ("search_first", "hard", "not_asked"),
    TEST: ("own_flight_preview", "closable_one_screen", "not_asked"),
    MATCHED_CONTROL: ("own_flight_preview", "hard_one_screen", "not_asked"),
    MATCHED_TEST: ("own_flight_preview", "soft_one_screen", "not_asked"),
}

ANNUAL_WEEKLY = "annual_weekly"
ANNUAL_WEEKLY_LIFETIME = "annual_weekly_lifetime"
OFFER_SCOPES = (ANNUAL_WEEKLY, ANNUAL_WEEKLY_LIFETIME)
# revision -> (config_version, offer scope). Owner decision 2026-10-21: Lifetime
# is shown in BOTH arms behind one remote switch, so the scope applies to both
# arms. Revisions start only by changing the backend setting; never reuse one.
REVISIONS = {
    1: ("closable_flow_v1", ANNUAL_WEEKLY),
    2: ("closable_flow_v2", ANNUAL_WEEKLY_LIFETIME),
    3: ("closable_flow_v3", ANNUAL_WEEKLY),            # relaunch after a kill or fix
    4: ("closable_flow_v4", ANNUAL_WEEKLY_LIFETIME),   # Lifetime after a relaunch
    5: ("matched_paywall_v1", ANNUAL_WEEKLY),
    6: ("matched_paywall_v2", ANNUAL_WEEKLY_LIFETIME),
}

ANNUAL_PRODUCT_ID = "com.zhirayr.Flighttracker.yearly.trial7d.6999"
WEEKLY_PRODUCT_ID = "com.zhirayr.Flighttracker.weekly"
LIFETIME_PRODUCT_ID = "com.zhirayr.Flighttracker.lifetime"
CURRENT_LIFETIME_PRODUCT_ID = "com.zhirayr.Flighttracker.lifetime.v2"
LIFETIME_PRODUCT_IDS = frozenset((LIFETIME_PRODUCT_ID, CURRENT_LIFETIME_PRODUCT_ID))

OPERATIONAL_OVERRIDES = ("forced_control", "lifetime_disabled")
# experience_action screens introduced for this test use fixed action sets.
EXPERIENCE_ACTIONS = {
    "paywall": frozenset(("retry_panel_shown", "retry_checkout_tapped", "retry_panel_closed")),
    "pending_flight": frozenset((
        "reminder_offer_shown", "reminder_opted_in", "reminder_declined", "reminder_turned_off",
        "card_removed", "track_tapped", "alerts_tapped", "live_activity_tapped", "copilot_tapped",
    )),
}
DEPARTURE_REMINDER_NOTIFICATION = "departure_paywall_reminder"
DEPARTURE_REMINDER_SOURCES = frozenset(("paywall_gated_departure_reminder", "departure_reminder"))
ONBOARDING_RATING_SOURCE = "onboarding_sharing"


def config_version(revision: int) -> str:
    return REVISIONS[revision][0]


def offer_scope(revision: int) -> str:
    return REVISIONS[revision][1]


def offers_lifetime(revision: int) -> bool:
    return revision in REVISIONS and offer_scope(revision) == ANNUAL_WEEKLY_LIFETIME


def variants(revision: int) -> tuple[str, str]:
    return (MATCHED_CONTROL, MATCHED_TEST) if revision >= 5 else VARIANTS


def fallback_variant(revision: int) -> str:
    return variants(revision)[0]


def settings_error(*, revision, lifetime_enabled, test_percent, nonproduction_test_percent,
                   enrollment_enabled=True) -> str | None:
    """Return a bounded reason when switches could reach an invalid enrollment.

    A Lifetime revision without its switch is allowed only while no new install
    can be randomized (both enrollment flags off, or the kill switch on). That
    is the emergency Lifetime kill for enrolled installs: they receive
    lifetime_delivery_enabled=false, and no new enrollment enters that revision
    without Lifetime. Resuming enrollment needs the next registry revision.
    """
    if isinstance(revision, bool) or revision not in REVISIONS:
        return "unknown_measurement_revision"
    if offers_lifetime(revision) and lifetime_enabled is not True and enrollment_enabled is not False:
        return "lifetime_revision_requires_lifetime_switch"
    for percent in (test_percent, nonproduction_test_percent):
        if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
            return "invalid_test_percent"
    if revision >= 5 and (test_percent != 50 or nonproduction_test_percent != 50):
        return "matched_paywall_requires_equal_allocation"
    return None


def bucket(installation_id) -> int:
    digest = hashlib.sha256(f"{EXPERIMENT_ID}:{str(installation_id).lower()}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def assigned_variant(installation_id, test_percent: int, revision: int = 1) -> str:
    control, test = variants(revision)
    return test if bucket(installation_id) < test_percent else control
