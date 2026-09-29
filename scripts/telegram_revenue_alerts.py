"""Isolated production revenue notifier. Never imports core or writes the app DB.

Telegram has no sendMessage idempotency key: ambiguous sends are held for review,
not automatically retried. Explicit Telegram rejections may be safely retried.
"""
import argparse
import fcntl
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib import error, request
from zoneinfo import ZoneInfo


class Rejected(Exception):
    def __init__(self, retry_after=300):
        self.retry_after = min(3600, max(60, int(retry_after)))


class Ambiguous(Exception):
    pass


def read_revenue(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return [dict(r) for r in db.execute('''SELECT id, product_id, purchase_date_ms,
            expires_date_ms, price_milliunits, currency, starts_trial, transaction_reason,
            revoked_date_ms FROM appstorerevenueevent WHERE purchase_environment='Production' ''')]


def kind(row):
    if row['revoked_date_ms'] is not None:
        return None
    if row['price_milliunits'] > 0:
        return 'payment'
    if row['starts_trial'] and row['price_milliunits'] == 0:
        return 'trial'
    return None


def local_time(ms):
    return datetime.fromtimestamp(ms / 1000, ZoneInfo('Asia/Yerevan')).strftime('%d %b %Y, %H:%M')


def format_message(row, now):
    event_kind = kind(row)
    if event_kind is None:
        raise ValueError('Not an alertable transaction')
    product = row['product_id']
    plan = 'Weekly' if product.endswith('.weekly') else 'Annual' if '.yearly' in product else 'Subscription'
    lines = ['✈️ Sofly — new free trial' if event_kind == 'trial' else '💳 Sofly — payment received',
             'Plan: ' + plan]
    if event_kind == 'payment':
        currency = row['currency'] if re.fullmatch(r'[A-Z]{3}', row['currency'] or '') else 'Unknown currency'
        amount = format(Decimal(row['price_milliunits']) / Decimal(1000), 'f')
        lines.append(f'Amount: {currency} {amount} (gross, before fees/tax)')
        lines.append('Type: renewal' if row['transaction_reason'] == 'RENEWAL' else 'Type: purchase')
    lines.append('Effective: ' + local_time(row['purchase_date_ms']) + ' Yerevan')
    if row['purchase_date_ms'] > now:
        lines.append('Apple reported this early; the paid period has not started yet.' if event_kind == 'payment'
                     else 'Apple reported this early; the trial has not started yet.')
    if row['expires_date_ms']:
        lines.append(('Trial ends: ' if event_kind == 'trial' else 'Period ends: ')
                     + local_time(row['expires_date_ms']) + ' Yerevan')
    lines.append('Apple-verified · Production')
    return '\n'.join(lines)


def open_state(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS seen (transaction_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, message TEXT NOT NULL,
          status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_ms INTEGER NOT NULL DEFAULT 0, telegram_message_id INTEGER);''')
    return db


def initialize(db, rows, now):
    if db.execute("SELECT 1 FROM meta WHERE key='started_ms'").fetchone():
        raise ValueError('Already initialized; refusing to reset delivery history')
    with db:
        db.execute("INSERT INTO meta VALUES ('started_ms',?)", (str(now),))
        db.executemany('INSERT INTO seen VALUES (?)', [(r['id'],) for r in rows])
        db.execute('INSERT INTO alerts(id,message,status) VALUES (?,?,?)',
                   ('system:enabled', '✅ Sofly trial and payment alerts enabled.\n'
                    'Apple-verified production transactions only. Existing transactions are excluded.', 'pending'))


def enqueue(db, rows, now):
    start = db.execute("SELECT value FROM meta WHERE key='started_ms'").fetchone()
    if start is None:
        raise ValueError('Initialize explicitly before enabling the worker')
    with db:
        for r in rows:
            # Allow a newly received incomplete transaction to be enriched later.
            # Initialization still excludes every pre-existing transaction ID.
            if r['purchase_date_ms'] >= int(start[0]) and not kind(r):
                continue
            # Re-signing, restores and webhook retries of known IDs never create
            # another alert. Historical purchases arriving after setup are excluded.
            inserted = db.execute('INSERT OR IGNORE INTO seen VALUES (?)', (r['id'],)).rowcount
            if inserted and r['purchase_date_ms'] >= int(start[0]) and kind(r):
                db.execute('INSERT INTO alerts(id,message,status) VALUES (?,?,?)',
                           (r['id'], format_message(r, now), 'pending'))


def telegram_send(text, token, chat_id):
    body = json.dumps({'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True}).encode()
    req = request.Request('https://api.telegram.org/bot' + token + '/sendMessage', data=body,
                          headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with request.urlopen(req, timeout=15) as response:
            payload = json.loads(response.read(65536))
    except error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(65536))
        except Exception:
            raise Ambiguous() from None
        if payload.get('ok') is False:
            raise Rejected(payload.get('parameters', {}).get('retry_after', 300)) from None
        raise Ambiguous() from None
    except Exception:
        # Never log URLs, bot tokens, response descriptions or exception strings.
        raise Ambiguous() from None
    if payload.get('ok') is False:
        raise Rejected(payload.get('parameters', {}).get('retry_after', 300))
    message_id = payload.get('result', {}).get('message_id')
    if payload.get('ok') is not True or type(message_id) is not int:
        raise Ambiguous()
    return message_id


def dispatch(db, rows, send, now):
    current = {r['id']: r for r in rows}
    with db:
        # A crashed process could have sent successfully before persisting the ack.
        db.execute("UPDATE alerts SET status='unknown' WHERE status='sending'")
    pending = db.execute("SELECT * FROM alerts WHERE status='pending' AND next_attempt_ms<=? ORDER BY rowid LIMIT 20", (now,)).fetchall()
    for alert in pending:
        if alert['id'] != 'system:enabled':
            row = current.get(alert['id'])
            if row is None or kind(row) is None:
                with db:
                    db.execute("UPDATE alerts SET status='suppressed' WHERE id=?", (alert['id'],))
                continue
            message = format_message(row, now)
        else:
            message = alert['message']
        with db:
            db.execute("UPDATE alerts SET status='sending', attempts=attempts+1 WHERE id=?", (alert['id'],))
        try:
            message_id = send(message)
        except Rejected as exc:
            with db:
                db.execute("UPDATE alerts SET status='pending',next_attempt_ms=? WHERE id=?", (now + exc.retry_after * 1000, alert['id']))
        except Exception:
            with db:
                db.execute("UPDATE alerts SET status='unknown' WHERE id=?", (alert['id'],))
        else:
            with db:
                db.execute("UPDATE alerts SET status='sent',telegram_message_id=? WHERE id=?", (message_id, alert['id']))
    return {r[0]: r[1] for r in db.execute('SELECT status,count(*) FROM alerts GROUP BY status')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--initialize', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if Path(args.db).resolve() == Path(args.state).resolve():
        raise ValueError('State must be separate from app database')
    with open(args.state + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'busy': True})); return
        if not args.initialize and not Path(args.state).is_file():
            raise ValueError('State missing; explicit initialization required')
        with closing(open_state(args.state)) as db:
            if args.status:
                print(json.dumps({r[0]: r[1] for r in db.execute('SELECT status,count(*) FROM alerts GROUP BY status')})); return
            rows = read_revenue(args.db)
            now = int(time.time() * 1000)
            if args.initialize:
                initialize(db, rows, now)
                print(json.dumps({'initialized': True, 'historical_rows_excluded': len(rows)})); return
            token, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
            if not token or not chat:
                raise ValueError('Telegram credentials unavailable')
            enqueue(db, rows, now)
            counts = dispatch(db, rows, lambda message: telegram_send(message, token, chat), now)
            print(json.dumps(counts))
            if counts.get('unknown') or counts.get('pending'):
                raise RuntimeError('Delivery requires retry or review')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'error': 'revenue_alert_worker_failed', 'inspect_delivery_state': True}))
        raise SystemExit(1)
