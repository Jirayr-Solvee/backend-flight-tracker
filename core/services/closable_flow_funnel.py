"""Daily derivation of the non-purged closable-flow funnel ledger.

Diagnostics are purged after 90 days, but close, return, retry and reminder
funnels are needed for the whole test and its D35 confirmation. One row per
closable enrollment is recomputed until it is final and never replaced by a
derivation that saw fewer events (for example after a purge).
"""

import json
from collections import Counter

from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, select

from .. import closable_flow_report as report
from ..activation_journey_contract import CLOSABLE_FLOW_ID
from ..models.activation_journey import ActivationJourneyDiagnosticContext, ActivationJourneyEnrollment
from ..models.closable_flow import ClosableFlowFunnelFact
from ..models.experiment import ExperimentDiagnosticEvent, current_time_ms

HORIZON_MS = report.FUNNEL_HORIZON_DAYS * report.DAY_MS
SETTLE_MS = report.FUNNEL_SETTLE_DAYS * report.DAY_MS


def _events(session, entry):
    rows = session.exec(select(ExperimentDiagnosticEvent, ActivationJourneyDiagnosticContext).join(
        ActivationJourneyDiagnosticContext, ActivationJourneyDiagnosticContext.id == ExperimentDiagnosticEvent.id,
    ).where(ActivationJourneyDiagnosticContext.enrollment_id == entry.id)).all()
    # Only events carrying this enrollment's exact frozen context and owner.
    return [{"event_name": event.event_name, "occurred_at_ms": event.occurred_at_ms,
             "paywall_presentation_id": event.paywall_presentation_id, "checkout_attempt_id": event.checkout_attempt_id,
             "properties": json.loads(event.properties_json)}
            for event, association in rows
            if association.user_id == entry.user_id and association.context_json == entry.context_json]


def refresh_closable_flow_funnel_facts(*, session: Session, now_ms=None, limit=report.ENTRY_CAP):
    """Return aggregate counts only; the caller commits."""
    now = current_time_ms() if now_ms is None else now_ms
    finished = select(ClosableFlowFunnelFact.id).where(ClosableFlowFunnelFact.final.is_(True))
    entries = session.exec(select(ActivationJourneyEnrollment).where(
        ActivationJourneyEnrollment.experiment_id == CLOSABLE_FLOW_ID,
        ActivationJourneyEnrollment.enrolled_at_ms <= now,
        ActivationJourneyEnrollment.id.not_in(finished),
    ).order_by(ActivationJourneyEnrollment.enrolled_at_ms, ActivationJourneyEnrollment.id).limit(limit)).all()
    counts = Counter(scanned=len(entries))
    for entry in entries:
        final = now >= entry.enrolled_at_ms + HORIZON_MS + SETTLE_MS
        existing = session.get(ClosableFlowFunnelFact, entry.id, populate_existing=True)
        derived = report.derive_funnel_fact(_events(session, entry), enrolled_at_ms=entry.enrolled_at_ms,
                                            horizon_ms=HORIZON_MS, through_ms=now)
        if existing is not None and derived["source_event_count"] < existing.source_event_count:
            # Fewer retained events than before: keep the earlier derivation.
            if final and not existing.final:
                existing.final = True
                session.add(existing)
                counts["finalized"] += 1
            counts["kept_previous_after_regression"] += 1
            continue
        values = {
            **derived, "id": entry.id, "experiment_id": entry.experiment_id,
            "measurement_revision": entry.measurement_revision, "variant": entry.variant,
            "analytics_environment": entry.analytics_environment, "assignment_source": entry.assignment_source,
            "randomized": entry.randomized, "enrolled_at_ms": entry.enrolled_at_ms,
            "horizon_days": report.FUNNEL_HORIZON_DAYS,
            "derived_through_ms": min(now, entry.enrolled_at_ms + HORIZON_MS), "final": final,
            # Events are purged 90 days after receipt, never before they occur.
            "retention_complete": (existing is None or existing.retention_complete)
                                  and entry.enrolled_at_ms >= now - report.DIAGNOSTIC_RETENTION_MS,
            "derived_at_ms": now,
        }
        statement = insert(ClosableFlowFunnelFact).values(**values)
        session.exec(statement.on_conflict_do_update(
            index_elements=["id"], set_=values,
            where=ClosableFlowFunnelFact.final.is_(False)
            & (ClosableFlowFunnelFact.source_event_count <= statement.excluded.source_event_count),
        ))
        counts["written"] += 1
        if final:
            counts["finalized"] += 1
    return dict(sorted(counts.items()))
