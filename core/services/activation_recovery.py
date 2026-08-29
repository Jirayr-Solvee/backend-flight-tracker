import logging
import time

from sqlmodel import Session, select

from ..models.activation_recovery import PurchaseActivationRecovery


logger = logging.getLogger(__name__)

RECOVERY_ALERT_DELAY_SECONDS = 5 * 60


def emit_due_activation_recovery_alerts(
    session: Session,
    *,
    now_seconds: int | None = None,
) -> int:
    """Emit each overdue recovery alert once and retain it for investigation."""

    current_time = now_seconds if now_seconds is not None else int(time.time())
    recoveries = list(
        session.exec(
            select(PurchaseActivationRecovery).where(
                PurchaseActivationRecovery.state == "recovery_pending",
                PurchaseActivationRecovery.alert_due_at <= current_time,
                PurchaseActivationRecovery.alerted_at.is_(None),
            )
        ).all()
    )

    for recovery in recoveries:
        logger.critical(
            "ACTIVATION_RECOVERY_OVERDUE transaction_id=%s user_id=%s flight_id=%s "
            "failure_reason=%s variant=%s app_version=%s build_number=%s "
            "first_pending_at=%s",
            recovery.transaction_id,
            recovery.user_id,
            recovery.flight_id,
            recovery.failure_reason,
            recovery.experiment_variant,
            recovery.app_version,
            recovery.build_number,
            recovery.first_pending_at,
        )
        recovery.alerted_at = current_time
        session.add(recovery)

    if recoveries:
        session.commit()
    return len(recoveries)
