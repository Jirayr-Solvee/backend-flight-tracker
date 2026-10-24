"""Minimal verified-subscription tables for the SQLite story sender tests."""


def create_subscription_tables(db):
    db.executescript('''
        CREATE TABLE usersubscriptionlink(user_id TEXT,subscription_id TEXT,
            PRIMARY KEY(user_id,subscription_id));
        CREATE TABLE "transaction"(id TEXT PRIMARY KEY,subscription_id TEXT,
            product_id TEXT,purchase_date INTEGER,signed_date INTEGER,
            expires_date INTEGER,revoked_date INTEGER,is_upgraded INTEGER,
            environment TEXT);
        CREATE TABLE appstoresubscriptionlifecycleevent(id TEXT PRIMARY KEY,
            original_transaction_id TEXT,transaction_id TEXT,purchase_environment TEXT,
            event_kind TEXT,notification_signed_date_ms INTEGER,renewal_signed_date_ms INTEGER,
            subscription_status INTEGER,grace_period_expires_date_ms INTEGER);
    ''')


def add_subscription(db, now_ms, user_id='owner', chain='chain', tx='transaction', **changes):
    values = dict(id=tx, subscription_id=chain, product_id='com.zhirayr.Flighttracker.yearly.trial7d.6999',
                  purchase_date=now_ms-86400000, signed_date=now_ms-86400000,
                  expires_date=now_ms+6*86400000, revoked_date=None, is_upgraded=0,
                  environment='Production')
    values.update(changes)
    db.execute('INSERT OR IGNORE INTO usersubscriptionlink VALUES (?,?)', (user_id,chain))
    db.execute('INSERT INTO "transaction" ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',tuple(values.values()))


def add_lifecycle(db, now_ms, identity='notice', chain='chain', tx='transaction', **changes):
    values = dict(id=identity, original_transaction_id=chain, transaction_id=tx,
                  purchase_environment='Production',event_kind='auto_renew_disabled',
                  notification_signed_date_ms=now_ms-1000,renewal_signed_date_ms=now_ms-1000,
                  subscription_status=1,grace_period_expires_date_ms=None)
    values.update(changes)
    db.execute('INSERT INTO appstoresubscriptionlifecycleevent ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',tuple(values.values()))
