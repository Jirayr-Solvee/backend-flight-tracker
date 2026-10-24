"""Read-only subscription validity for background story delivery.

user.premium_valid_until is a short API-access lease, refreshed by foreground
activity. It must not determine whether an otherwise valid subscriber can be
re-engaged. Only server-verified, currently linked Production facts grant access
here; this never renews that API lease or writes subscription state.
"""

_AUTHORITATIVE_KINDS = (
    'renewal', 'expiration', 'grace_period_started', 'grace_period_expired',
    'billing_failure', 'refund', 'refund_reversed', 'revocation',
)
_ENDED_KINDS = ('expiration', 'grace_period_expired', 'refund', 'revocation', 'billing_failure')


def has_subscription_access(db, user_id, now_ms):
    """Return whether a linked verified subscription grants access at now_ms.

    Caller supplies a sqlite3.Row connection to the authoritative app database.
    Missing/corrupt storage raises: the worker fails closed rather than falling
    back to a cache. Each call uses one SELECT snapshot, including lifecycle state,
    so a concurrent refund cannot be lost between transaction and lifecycle reads.
    Call again immediately before sending, after the reservation has committed.
    """
    if not user_id or type(now_ms) is not int:
        return False
    placeholders = ','.join('?' for _ in _AUTHORITATIVE_KINDS)
    ended = ','.join('?' for _ in _ENDED_KINDS)
    # Future-effective renewals can arrive hours early. Keep the current period
    # until its successor is effective; never treat receipt time as access time.
    rows = db.execute(f'''
        WITH ranked AS (
            SELECT t.*, ROW_NUMBER() OVER (
                PARTITION BY t.subscription_id
                ORDER BY t.purchase_date DESC, COALESCE(t.signed_date, 0) DESC, t.id DESC
            ) AS position
            FROM "transaction" t
            JOIN usersubscriptionlink link ON link.subscription_id=t.subscription_id
            WHERE link.user_id=? AND t.environment='Production'
              AND typeof(t.purchase_date)='integer' AND t.purchase_date>0
              AND t.purchase_date<=?
        )
        SELECT t.id,t.expires_date,t.revoked_date,t.is_upgraded,
               life.event_kind,life.subscription_status,life.grace_period_expires_date_ms
        FROM ranked t
        LEFT JOIN appstoresubscriptionlifecycleevent life ON life.id=(
            SELECT e.id FROM appstoresubscriptionlifecycleevent e
            WHERE e.original_transaction_id=t.subscription_id
              AND e.purchase_environment='Production'
              AND (e.renewal_signed_date_ms IS NOT NULL OR e.event_kind IN ({placeholders}))
              AND (e.transaction_id=t.id OR
                   (e.transaction_id IS NULL AND e.notification_signed_date_ms>=t.purchase_date))
            ORDER BY COALESCE(e.notification_signed_date_ms,0) DESC,
                     CASE WHEN e.event_kind IN ({ended}) OR e.subscription_status NOT IN (1,4)
                          THEN 1 ELSE 0 END DESC,
                     COALESCE(e.renewal_signed_date_ms,0) DESC, e.id DESC
            LIMIT 1
        )
        WHERE t.position=1
    ''', (user_id, now_ms, *_AUTHORITATIVE_KINDS, *_ENDED_KINDS)).fetchall()
    for row in rows:
        if row['revoked_date'] is not None or row['is_upgraded']:
            continue
        # Turning auto-renew off is not an expiry. Conversely, an authoritative
        # expiration/refund must not be overridden by a future date on the JWS.
        if row['event_kind'] in _ENDED_KINDS or row['subscription_status'] not in (None, 1, 4):
            continue
        expiry = row['expires_date']
        if type(expiry) is not int:
            continue  # Non-subscriptions/missing expiry do not grant access.
        grace = row['grace_period_expires_date_ms']
        if type(grace) is int:
            expiry = max(expiry, grace)
        if expiry > now_ms:
            return True
    return False
