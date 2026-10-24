"""Persistent, budgeted AI attempts. Never store provider bodies or exception text."""
import hashlib
from datetime import timedelta
from email.utils import parsedate_to_datetime

from .cockpit_stories import parse_time, utc_string

MAX_ATTEMPTS = 3
TRANSIENT_HTTP = frozenset((429, 500, 502, 503, 504))
KNOWN_HTTP = frozenset((400, 401, 402, 403, 404, 408, 409, 413, 422, 429, 500, 501, 502, 503, 504))
REVIEW_REASONS = ('none', 'safety', 'medical_security', 'personal_information',
                  'unclear_meaning', 'alarming_claim', 'other')
REJECTION_REASONS = ('none', 'encoded_data', 'bulletin', 'routine_telemetry',
                     'no_actual_event', 'low_interest', 'other')
KINDS = frozenset(('safety','strike','crew','cabin','cargo','weather','other','route','ground'))


def init_state(db):
    db.execute('''CREATE TABLE IF NOT EXISTS cockpit_ai_attempt (
        id TEXT PRIMARY KEY, received TEXT NOT NULL, kind TEXT NOT NULL,
        attempts INTEGER NOT NULL, attempted_at TEXT NOT NULL,
        next_attempt_at TEXT, last_reason TEXT NOT NULL)''')


def cooling_down(db, now):
    row=db.execute("SELECT value FROM cockpit_metadata WHERE key='ai-not-before'").fetchone()
    return bool(row and (stamp:=parse_time(row[0])) and stamp>now)


def claim(db, key, now, entries, kind, freshness_hours):
    """Claim and debit all budgets in one transaction, across independent workers.

    A killed process leaves status=attempted. It is never automatically replayed.
    Only a received explicit transient HTTP error may return it to pending.
    """
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT status,received FROM cockpit_queue WHERE id=?',(key,)).fetchone()
        attempt=db.execute('SELECT attempts,next_attempt_at FROM cockpit_ai_attempt WHERE id=?',(key,)).fetchone()
        if (not row or row[0]!='pending' or kind not in KINDS
                or not (stamp:=parse_time(row[1])) or stamp<now-timedelta(hours=freshness_hours)
                or cooling_down(db,now)
                or (attempt and (attempt[0]>=MAX_ATTEMPTS or
                    (attempt[1] and (due:=parse_time(attempt[1])) and due>now)))):
            db.rollback();return False
        for bucket,amount,limit in entries:
            used=db.execute('SELECT used FROM cockpit_budget WHERE bucket=?',(bucket,)).fetchone()
            if (used[0] if used else 0)+amount>limit+1e-9:
                db.rollback();return False
        for bucket,amount,_ in entries:
            db.execute('INSERT INTO cockpit_budget VALUES (?,?) ON CONFLICT(bucket) DO UPDATE SET used=used+excluded.used',(bucket,amount))
        db.execute("UPDATE cockpit_queue SET status='attempted' WHERE id=?",(key,))
        db.execute('''INSERT INTO cockpit_ai_attempt VALUES (?,?,?,?,?,NULL,'attempted')
            ON CONFLICT(id) DO UPDATE SET attempts=attempts+1,attempted_at=excluded.attempted_at,
            next_attempt_at=NULL,last_reason='attempted' ''',
            (key,row[1],kind,1,utc_string(now)))
        db.commit();return True
    except Exception:
        db.rollback();raise


def retry_delay(response, key, attempts, now):
    # One or two future scheduler windows, with stable per-item jitter.
    delay=600*2**max(0,min(attempts-1,2))+int(hashlib.sha256(key.encode()).hexdigest()[:4],16)%61
    raw=response.headers.get('Retry-After','')
    try:
        requested=float(raw) if raw.isdigit() else (parsedate_to_datetime(raw)-now).total_seconds()
        delay=max(delay,min(86400,max(0,requested)))
    except (ValueError,TypeError,OverflowError):pass
    return timedelta(seconds=delay)


def http_failure(db, key, response, now, freshness_hours):
    """Retry only known transient responses, later and within all original caps.

    Never inspect or log the response body, URL, request or exception message.
    A cooldown also prevents burning the other slot against the same outage.
    """
    code=response.status_code
    reason='http_'+str(code) if code in KNOWN_HTTP else 'http_other'
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT attempts,received FROM cockpit_ai_attempt WHERE id=?',(key,)).fetchone()
        assert row is not None
        due=now+retry_delay(response,key,row[0],now)
        retry=(code in TRANSIENT_HTTP and row[0]<MAX_ATTEMPTS
               and due<parse_time(row[1])+timedelta(hours=freshness_hours))
        db.execute("UPDATE cockpit_queue SET status=?,payload=CASE WHEN ? THEN payload ELSE '{}' END WHERE id=?",
                   ('pending' if retry else 'failed',retry,key))
        db.execute('UPDATE cockpit_ai_attempt SET next_attempt_at=?,last_reason=? WHERE id=?',
                   (utc_string(due) if retry else None,reason,key))
        if code in TRANSIENT_HTTP or code in (400,401,402,403,404,413,422):
            if code not in TRANSIENT_HTTP:due=max(due,now+timedelta(hours=1))
            previous=db.execute("SELECT value FROM cockpit_metadata WHERE key='ai-not-before'").fetchone()
            if previous and (stamp:=parse_time(previous[0])):due=max(due,stamp)
            db.execute("INSERT OR REPLACE INTO cockpit_metadata VALUES ('ai-not-before',?)",(utc_string(due),))
        db.commit();return reason,retry
    except Exception:
        db.rollback();raise
