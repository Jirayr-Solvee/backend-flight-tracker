"""Atomic last-confirmed-answer ordering for old and versioned clients."""

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.dialects.sqlite import insert

from ..models.experiment import (
    ExperimentExposure, ExperimentGoalConfirmation, ExperimentGoalConfirmationReceipt,
    ExperimentGoalSelection,
)


def apply_goal_confirmation(*, session, data, user) -> dict:
    exposure_id = f"{data.experiment.experiment_id}:{data.experiment.installation_id}"
    ledger = ExperimentGoalConfirmation
    # A write statement, even for an existing row, serializes subsequent reads
    # with other writers. Never start with a read-then-unconditional update.
    session.exec(insert(ledger).values(id=exposure_id).on_conflict_do_nothing())
    stored = session.get(ledger, exposure_id, populate_existing=True)
    previous = session.get(ExperimentGoalSelection, exposure_id, populate_existing=True)
    exposure = session.get(ExperimentExposure, exposure_id, populate_existing=True)
    # Signing into an existing account does not merge guest ownership. A known
    # installation/exposure UUID is not authorization to change another user's
    # answer. The initial guard write rolls back with this rejected request.
    if any(row is not None and row.user_id != user.id for row in (previous, exposure)):
        raise HTTPException(status_code=403, detail="Goal selection belongs to another account")
    if previous and not stored.payload_sha256:
        # Existing installations are migrated lazily without changing their
        # captured timestamp, cohort metadata or answer.
        stored.selected_at_ms = previous.selected_at_ms
        stored.payload_sha256 = _legacy_hash(previous.selected_goal_keys, previous.selected_at_ms)
        session.add(stored)
        session.flush()

    revision = data.confirmation_revision or 0
    confirmation_id = str(data.confirmation_id) if data.confirmation_id else None
    payload = data.model_dump(mode="json")
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = (hashlib.sha256(payload_json.encode()).hexdigest() if revision else
              _legacy_hash(",".join(data.selected_goal_keys), data.selected_at_ms))
    receipt_id = f"{exposure_id}:{confirmation_id}" if confirmation_id else None
    receipt = session.get(ExperimentGoalConfirmationReceipt, receipt_id) if receipt_id else None
    if receipt is not None and (receipt.payload_sha256 != digest
                                or receipt.confirmation_revision != revision
                                or receipt.user_id != user.id):
        raise HTTPException(status_code=409, detail="Goal confirmation identity conflict")
    if (confirmation_id and stored.confirmation_id == confirmation_id
            and stored.payload_sha256 != digest):
        raise HTTPException(status_code=409, detail="Goal confirmation identity conflict")

    values = dict(id=exposure_id, confirmation_revision=revision,
                  confirmation_id=confirmation_id, selected_at_ms=data.selected_at_ms,
                  payload_sha256=digest, capture_payload_json=payload_json)
    statement = insert(ledger).values(**values)
    incoming = statement.excluded
    newer = or_(
        ledger.payload_sha256 == "",
        incoming.confirmation_revision > ledger.confirmation_revision,
        and_(incoming.confirmation_revision == 0, ledger.confirmation_revision == 0,
             incoming.selected_at_ms > ledger.selected_at_ms),
    )
    statement = statement.on_conflict_do_update(
        index_elements=["id"], set_=values, where=newer,
    )
    accepted = session.exec(statement.returning(ledger.id)).scalar_one_or_none() is not None
    session.refresh(stored)
    if accepted:
        if receipt_id:
            session.exec(insert(ExperimentGoalConfirmationReceipt).values(
                id=receipt_id, exposure_id=exposure_id, confirmation_id=confirmation_id,
                confirmation_revision=revision, user_id=user.id, payload_sha256=digest,
            ).on_conflict_do_nothing())
        outcome = "accepted"
    elif stored.payload_sha256 == digest:
        outcome = "idempotent"
    else:
        equal = (revision == stored.confirmation_revision and
                 (revision > 0 or data.selected_at_ms == stored.selected_at_ms))
        if equal:
            raise HTTPException(status_code=409, detail="Goal confirmation revision conflict")
        outcome = "stale"
    return dict(status=outcome, request_confirmation_id=confirmation_id,
                accepted_revision=stored.confirmation_revision,
                accepted_confirmation_id=stored.confirmation_id)


def _legacy_hash(keys: str, selected_at_ms: int) -> str:
    return hashlib.sha256(f"{selected_at_ms}:{keys}".encode()).hexdigest()
