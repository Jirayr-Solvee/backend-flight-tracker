"""Validate exact synthetic Swift-encoded JSON through backend DTOs/routes.

Run from a fresh temporary directory with an empty environment and repository
PYTHONPATH. This verifier installs test-only configuration before importing core,
uses in-memory SQLite, and never verifies a real JWS or contacts an API/provider.
"""

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

from tests import test_experiment_reporting as fixtures

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from core.activation_journey_contract import ActivationJourneyAssignmentRequest, ActivationJourneyContext, ActivationJourneyEnrollmentRequest
from core.config import settings
from core.dependency import get_current_user
from core.models import get_session
from core.models.activation_journey import ActivationJourneyEnrollment, ActivationJourneyGoalSelection
from core.models.experiment import ExperimentDiagnosticEvent
from core.models.user import User
from core.routers.activation_journey import router as journey_router
from core.routers.experiment_diagnostics import DiagnosticEvent, router as diagnostic_router
from core.routers.subscriptions import CreateTransactionRequest, ExperimentGoalSelectionRequest, router as subscription_router


def main(directory):
    directory = Path(directory)
    models = {"assignment": ActivationJourneyAssignmentRequest, "journey": ActivationJourneyContext,
              "enrollment": ActivationJourneyEnrollmentRequest, "enrollment-event": DiagnosticEvent,
              "checkout-event": DiagnosticEvent, "goals": ExperimentGoalSelectionRequest, "payment-shape": CreateTransactionRequest}
    raw, parsed, checks, hashes = {}, {}, {}, {}
    for name, model in models.items():
        raw[name] = (directory / f"{name}.json").read_bytes()
        hashes[name] = hashlib.sha256(raw[name]).hexdigest()
        parsed[name] = model.model_validate_json(raw[name])
        checks[f"{name}_strict_contract"] = True
    # The new candidate is additional exact Swift-encoded context. The existing
    # HTTP fixture intentionally remains the historical goals journey; do not
    # rewrite that installation's captured arm to make candidate QA pass.
    candidate_path = directory / "search-first-flight-detail-journey.json"
    candidate_contract_verified = False
    if candidate_path.is_file():
        candidate_bytes = candidate_path.read_bytes()
        hashes["search-first-flight-detail-journey"] = hashlib.sha256(candidate_bytes).hexdigest()
        candidate = ActivationJourneyContext.model_validate_json(candidate_bytes)
        checks["candidate_swift_context_strict_contract"] = (
            candidate.variant == "search_first_flight_detail"
            and candidate.intended_onboarding == "search_first"
            and candidate.intended_paywall == "flight_detail"
            and candidate.goals_status == "not_asked"
        )
        candidate_contract_verified = checks["candidate_swift_context_strict_contract"]
    payment_journey = ActivationJourneyContext.model_validate(parsed["payment-shape"].journey)
    checks["payment_journey_post_commit_contract"] = payment_journey == parsed["journey"]
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        user = User(id="synthetic-wire-owner")
        session.add(user)
        session.commit()
        app = FastAPI()
        for router in (journey_router, diagnostic_router, subscription_router):
            app.include_router(router, prefix="/subscriptions")
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_session] = lambda: session
        client = TestClient(app)
        with patch.object(settings, "ACTIVATION_JOURNEY_NONPRODUCTION_ENROLLMENT_ENABLED", True), \
             patch.object(settings, "ACTIVATION_JOURNEY_SEARCH_FIRST_PERCENT", 0), \
             patch.object(settings, "ACTIVATION_JOURNEY_FLIGHT_DETAIL_VARIANT", "goals_flight_detail"), \
             patch.object(settings, "ACTIVATION_JOURNEY_CONFIG_VERSION", parsed["journey"].config_version), \
             patch("core.services.activation_journey.current_time_ms", return_value=parsed["journey"].enrolled_at_ms + 1000):
            response = client.post("/subscriptions/activation-journey/assignment", content=raw["assignment"], headers={"Content-Type": "application/json"})
            checks["exact_assignment_http_200"] = response.status_code == 200
            checks["server_response_matches_swift_frozen_context"] = ActivationJourneyContext.model_validate(response.json()["journey"]) == parsed["journey"]
            response = client.post("/subscriptions/activation-journey/enrollment", content=raw["enrollment"], headers={"Content-Type": "application/json"})
            checks["exact_enrollment_http_200"] = response.status_code == 200
            for name in ("enrollment-event", "checkout-event"):
                # Preserve the exact event bytes; the HTTP endpoint wraps events
                # in a batch but does not re-encode or normalize their fields.
                response = client.post("/subscriptions/experiments/events", content=b'{"events":[' + raw[name] + b']}', headers={"Content-Type": "application/json"})
                checks[f"{name}_exact_http_200"] = response.status_code == 200
            response = client.post("/subscriptions/experiments/goals", content=raw["goals"], headers={"Content-Type": "application/json"})
            checks["exact_goals_http_200"] = response.status_code == 200
        checks["one_canonical_enrollment"] = len(session.exec(select(ActivationJourneyEnrollment)).all()) == 1
        checks["one_final_goal_confirmation"] = len(session.exec(select(ActivationJourneyGoalSelection)).all()) == 1
        checks["two_strict_diagnostics"] = len(session.exec(select(ExperimentDiagnosticEvent)).all()) == 2
    engine.dispose()
    print(json.dumps({"source": "Actual Swift JSONEncoder output, exact input bytes", "sha256": hashes,
                      "candidate_context_contract_verified": candidate_contract_verified,
                      "checks": checks, "all_passed": all(checks.values()),
                      "scope": "Local strict DTO and authenticated historical-goals HTTP contract proof, plus candidate context DTO when its additive fixture is present. Candidate assignment/enrollment/goal HTTP behavior is covered by the unit suite, not by rewriting historical Swift fixture bytes. Synthetic JWS intentionally not verified; no TestFlight/device/network delivery proof."}, sort_keys=True))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
