"""Verify the actual Swift enum/codec fixture through strict DTOs and local HTTP.

Usage from a fresh temporary directory and an empty environment, with repository
PYTHONPATH: python /absolute/repo/scripts/verify_analytics_wire.py events.json coverage.json
Only synthetic fixture credentials and in-memory SQLite are used. No SDK,
StoreKit, provider, APNs, production request or device-delivery proof is implied.
"""

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import get_args
from unittest.mock import patch
from uuid import UUID


def ensure_isolated_execution(repository):
    # macOS can inject its text-encoding hint even into an env -i interpreter.
    allowed_environment = {"PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    if set(os.environ) - allowed_environment or os.environ.get("PYTHONPATH") != str(repository):
        raise SystemExit("Run with env -i and the exact repository PYTHONPATH; inherited app settings are forbidden")
    if Path.cwd() == repository or Path("database.db").exists() or Path(".env").exists():
        raise SystemExit("Run from a fresh temporary working directory, not an app/database directory")


def event_fragments(raw):
    """Keep each Swift JSON object's original bytes inside the HTTP batch."""
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text) and text[index].isspace():
        index += 1
    if text[index:index + 1] != "[":
        raise ValueError("Expected the Swift event array")
    index += 1
    while True:
        while index < len(text) and (text[index].isspace() or text[index] == ","):
            index += 1
        if text[index:index + 1] == "]":
            return
        start = index
        _, index = decoder.raw_decode(text, index)
        yield text[start:index].encode("utf-8")


def main(events_path, coverage_path):
    events_path, coverage_path = Path(events_path).resolve(), Path(coverage_path).resolve()
    repository = Path(__file__).resolve().parents[1]
    ensure_isolated_execution(repository)

    # The fixture supplies harmless settings before core's existing model import
    # creates its cwd-local schema. Never import it in a real database directory.
    from tests import test_experiment_reporting as fixtures  # noqa: F401
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import TypeAdapter, ValidationError
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine, select
    from core.activation_journey_contract import ActivationJourneyAssignmentRequest
    from core.config import settings
    from core.models import get_session
    from core.models.apple_ads import AppStoreRevenueEvent
    from core.models.experiment import ExperimentDiagnosticEvent
    from core.models.transaction import Transaction
    from core.models.user import User
    from core.routers.activation_journey import assignment
    from core.routers.experiment_diagnostics import DiagnosticEvent, EventName, router
    from core.utils import create_jwt

    raw = events_path.read_bytes()
    coverage_raw = coverage_path.read_bytes()
    coverage = json.loads(coverage_raw)
    errors = []
    try:
        events = TypeAdapter(list[DiagnosticEvent]).validate_json(raw)
    except ValidationError as error:
        # Return locations/types only: never echo rejected arbitrary input.
        errors = [{"location": list(item["loc"]), "type": item["type"]}
                  for item in error.errors(include_input=False, include_context=False)]
        print(json.dumps({"all_passed": False, "dto_errors": errors}, sort_keys=True))
        return 1
    fragments = list(event_fragments(raw))
    names = {event.event_name for event in events}
    schema_counts = dict(Counter(str(event.properties.event_schema_version) for event in events))
    declared_schema_counts = coverage.get("schema_counts", {str(coverage["schema"]): len(events)})
    # Swift commonly emits uppercase UUIDs; Pydantic normalizes them. Identity
    # comparisons must use the UUID value while HTTP retains original bytes.
    universal_ids = {str(UUID(value)) for value in coverage.get("universal_event_ids", [])}
    historical_ids = {str(UUID(value)) for value in coverage.get("historical_replay_event_ids", [])}
    by_id = {str(event.event_id): event for event in events}
    checks = {
        "all_exact_swift_rows_strict_dto": bool(events) and len(events) == len(fragments),
        "swift_declared_row_count": coverage["fixture_rows"] == len(events),
        "swift_declared_names_match_payload": set(coverage["covered_event_names"]) == names,
        "client_and_server_name_sets_exact": set(get_args(EventName)) == names,
        "client_allowlist_has_no_uncovered_names": not coverage["backend_excluded_names"]
            and coverage["backend_allowlisted_names"] == len(names),
        "capture_schema_matches_manifest": schema_counts == declared_schema_counts,
        "current_capture_schema_is_present": str(coverage["schema"]) in schema_counts,
        "fixture_captured_ids_are_unique": len(by_id) == len(events),
        "scenario_ids_match_fixture": universal_ids <= by_id.keys() and historical_ids <= by_id.keys()
            and not universal_ids & historical_ids,
        "universal_capture_has_no_experiment_or_qa": all(
            by_id[event_id].experiment is None and by_id[event_id].journey is None
            and by_id[event_id].properties.event_schema_version == coverage["schema"]
            and by_id[event_id].properties.layout == "compact_flight_detail"
            and by_id[event_id].properties.effective_offer != "flight_detail_treatment"
            for event_id in universal_ids if event_id in by_id
        ),
        "universal_offer_scope_stays_selected_onboarding_only": all(
            (
                by_id[event_id].properties.paywall_surface == "selected_flight"
                and by_id[event_id].properties.source == "universal_onboarding"
                and by_id[event_id].properties.effective_onboarding == "search_first"
                and by_id[event_id].properties.offer_context == "selected_flight_4999"
                and by_id[event_id].properties.assigned_product_id == "com.zhirayr.Flighttracker.yearly.trial7d.4999"
                and by_id[event_id].properties.effective_offer is None
            ) or (
                by_id[event_id].properties.paywall_surface == "other"
                and by_id[event_id].properties.source in ("number_search", "airport_search", "global_map")
                and by_id[event_id].properties.offer_context == "standard_annual_offer"
                and by_id[event_id].properties.assigned_product_id == "com.zhirayr.Flighttracker.yearly.trial7d"
                and by_id[event_id].properties.effective_offer == "standard"
            )
            for event_id in universal_ids if event_id in by_id
        ),
        "historical_replay_keeps_schema20_and_original_protocol": all(
            by_id[event_id].properties.event_schema_version == 20
            and (by_id[event_id].experiment is not None or by_id[event_id].journey is not None)
            for event_id in historical_ids if event_id in by_id
        ),
    }

    # The synthetic fixture deliberately reuses an installation across legacy
    # and new-protocol examples. Isolate those examples; never weaken the real
    # migration lock to make a mixed synthetic fixture pass in one database.
    groups = defaultdict(list)
    for event, fragment in zip(events, fragments):
        context = event.journey or event.experiment
        key = ("journey" if event.journey else "legacy" if event.experiment else "cohortless",
               context.model_dump_json() if context else str(event.installation_id))
        groups[key].append((event, fragment))
    accepted = retried = 0
    for group_number, ((protocol, _), group) in enumerate(groups.items()):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(engine)
        user_id = f"synthetic-analytics-wire-owner-{group_number}"
        with Session(engine) as session:
            user = User(id=user_id)
            session.add(user)
            session.commit()
            context = group[0][0].journey
            if context and context.assignment_source in ("server_assignment", "server_disabled"):
                request = ActivationJourneyAssignmentRequest(
                    **{name: getattr(context, name) for name in (
                        "installation_id", "enrollment_event_id", "enrolled_at_ms", "app_version", "build_number", "analytics_environment")},
                    is_new_installation=True,
                )
                with patch.object(settings, "ACTIVATION_JOURNEY_PRODUCTION_ENROLLMENT_ENABLED", context.eligible), \
                     patch.object(settings, "ACTIVATION_JOURNEY_NONPRODUCTION_ENROLLMENT_ENABLED", context.eligible), \
                     patch.object(settings, "ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT", 100 if context.variant == "search_first_standard" else 0), \
                     patch.object(settings, "ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT", context.variant if context.variant != "search_first_standard" else "goals_flight_detail"), \
                     patch.object(settings, "ACTIVATION_JOURNEY_CONFIG_VERSION", context.config_version), \
                     patch("core.services.activation_journey.current_time_ms", return_value=context.enrolled_at_ms + 1000):
                    checks[f"group_{group_number}_actual_server_proposal_matches"] = assignment(request, user, session)["journey"] == context

        app = FastAPI()
        app.include_router(router, prefix="/subscriptions")
        def sessions():
            with Session(engine) as session:
                yield session
        app.dependency_overrides[get_session] = sessions
        now = max(event.occurred_at_ms for event, _ in group) + 1000
        with TestClient(app) as client, \
             patch("core.routers.experiment_diagnostics.current_time_ms", return_value=now), \
             patch("core.services.activation_journey.current_time_ms", return_value=now):
            path = "/subscriptions/experiments/events"
            headers = {"Authorization": "Bearer " + create_jwt(sub=user_id), "Content-Type": "application/json"}
            group_ok = True
            for event, fragment in group:
                body = b'{"events":[' + fragment + b']}'
                response = client.post(path, content=body, headers=headers)
                row_ok = response.status_code == 200 and response.json().get("accepted") == 1
                if response.status_code == 200:
                    accepted += response.json()["accepted"]
                retry = client.post(path, content=body, headers=headers)
                row_ok &= retry.status_code == 200 and retry.json().get("duplicates") == 1 and retry.json().get("accepted") == 0
                group_ok &= row_ok
                if retry.status_code == 200:
                    retried += retry.json()["duplicates"]
                if not row_ok:
                    errors.append({"event_name": event.event_name, "http_status": response.status_code, "retry_status": retry.status_code})
            checks[f"group_{group_number}_{protocol}_authenticated_http_and_exact_retry"] = group_ok
            private = json.loads(group[0][1])
            private["properties"]["query"] = "synthetic private query"
            checks[f"group_{group_number}_unallowlisted_query_rejected"] = client.post(path, json={"events": [private]}, headers=headers).status_code == 422
            checks[f"group_{group_number}_ingest_auth_required"] = client.post(path, content=b'{"events":[' + group[0][1] + b']}', headers={"Content-Type": "application/json"}).status_code in (401, 403)
        with Session(engine) as session:
            checks[f"group_{group_number}_one_row_per_captured_id"] = len(session.exec(select(ExperimentDiagnosticEvent)).all()) == len(group)
            checks[f"group_{group_number}_diagnostics_create_no_verified_money"] = not session.exec(select(Transaction)).all() and not session.exec(select(AppStoreRevenueEvent)).all()
        engine.dispose()

    result = {
        "all_passed": all(checks.values()), "schema": coverage["schema"],
        "schema_counts": schema_counts,
        "universal_fixture_rows": len(universal_ids), "historical_replay_fixture_rows": len(historical_ids),
        "swift_enum_cases": coverage["enum_cases"], "fixture_rows": len(events),
        "supported_event_names": len(names), "authenticated_http_accepted": accepted,
        "exact_http_retries_deduplicated": retried, "isolated_context_groups": len(groups),
        "sha256": {"events.json": hashlib.sha256(raw).hexdigest(), "coverage.json": hashlib.sha256(coverage_raw).hexdigest()},
        "checks": checks, "failures": errors,
        "scope": "Exact synthetic Swift enum/codec JSON through strict DTOs and isolated authenticated in-memory HTTP routes. No production, SDK, StoreKit verification, runtime-emitter timing, device or network delivery proof.",
    }
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events")
    parser.add_argument("coverage")
    args = parser.parse_args()
    raise SystemExit(main(args.events, args.coverage))
