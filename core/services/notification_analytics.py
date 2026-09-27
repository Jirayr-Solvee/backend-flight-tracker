"""First-party notification copy, encrypted at rest; no provider analytics/logs."""
import base64
import hashlib
import json
import re
from cryptography.fernet import Fernet, InvalidToken
from ..config import settings

def _cipher():
    key = hashlib.sha256((settings.JWT_SECRET + ':notification-copy:v1').encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))

def seal_copy(copy):
    # Copy can contain forwarded itinerary/contact details. Preserve product
    # wording but remove contacts/URLs before first-party storage and reports.
    def redact(value):
        for pattern in (r'https?://\S+', r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', r'(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)'):
            value = re.sub(pattern, '[redacted]', value)
        return value
    clear = json.dumps({k: redact(v) for k, v in copy.items()}, sort_keys=True, ensure_ascii=False).encode()
    return _cipher().encrypt(clear).decode(), hashlib.sha256(clear).hexdigest()

def open_copy(ciphertext):
    try:
        return json.loads(_cipher().decrypt(ciphertext.encode()))
    except (InvalidToken, ValueError, TypeError, AttributeError):
        return None

def remove_notification_diagnostics(session, user_id):
    """Part of the caller's account-deletion transaction; never commit here."""
    from sqlalchemy import delete
    from sqlmodel import select
    from ..models.experiment import ExperimentDiagnosticEvent
    from ..models.activation_journey import ActivationJourneyDiagnosticContext
    ids = select(ExperimentDiagnosticEvent.id).where(
        ExperimentDiagnosticEvent.user_id == user_id,
        ExperimentDiagnosticEvent.event_name == 'push_opened')
    session.exec(delete(ActivationJourneyDiagnosticContext).where(ActivationJourneyDiagnosticContext.id.in_(ids)))
    session.exec(delete(ExperimentDiagnosticEvent).where(
        ExperimentDiagnosticEvent.user_id == user_id,
        ExperimentDiagnosticEvent.event_name == 'push_opened'))
