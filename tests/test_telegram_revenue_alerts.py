import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/telegram_revenue_alerts.py'
spec = importlib.util.spec_from_file_location('telegram_revenue_alerts', SCRIPT)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
NOW = 1790740800000


def revenue(identifier='new', **overrides):
    row = dict(id=identifier, product_id='sofly.yearly', purchase_date_ms=NOW + 1,
               expires_date_ms=NOW + 604800000, price_milliunits=0, currency='USD',
               starts_trial=True, transaction_reason='PURCHASE', revoked_date_ms=None)
    row.update(overrides)
    return row


class RevenueAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = str(Path(self.tmp.name) / 'state.db')
        self.db = worker.open_state(self.state)
        self.addCleanup(self.db.close)
        worker.initialize(self.db, [revenue('old')], NOW)
        # Acknowledge setup independently; transaction assertions exclude it.
        worker.dispatch(self.db, [], lambda _: 1, NOW)

    def status(self, identifier):
        row = self.db.execute('SELECT status FROM alerts WHERE id=?', (identifier,)).fetchone()
        return row[0] if row else None

    def test_baseline_and_duplicate_updates_excluded(self):
        rows = [revenue('old'), revenue()]
        worker.enqueue(self.db, rows, NOW + 2)
        sent = []
        worker.dispatch(self.db, rows, lambda message: sent.append(message) or 2, NOW + 2)
        worker.enqueue(self.db, rows, NOW + 3)
        worker.dispatch(self.db, rows, lambda message: sent.append(message) or 3, NOW + 3)
        self.assertEqual(len(sent), 1)
        self.assertIsNone(self.status('old'))
        self.assertEqual(self.status('new'), 'sent')

    def test_late_historical_transaction_excluded(self):
        worker.enqueue(self.db, [revenue(purchase_date_ms=NOW - 1)], NOW + 2)
        self.assertIsNone(self.status('new'))

    def test_classification(self):
        self.assertEqual(worker.kind(revenue()), 'trial')
        self.assertEqual(worker.kind(revenue(price_milliunits=49990)), 'payment')
        self.assertIsNone(worker.kind(revenue(starts_trial=False)))
        self.assertIsNone(worker.kind(revenue(revoked_date_ms=NOW)))
        self.assertIsNone(worker.kind(revenue(price_milliunits=-1)))

    def test_incomplete_new_transaction_can_be_enriched(self):
        worker.enqueue(self.db, [revenue(starts_trial=False)], NOW + 2)
        self.assertIsNone(self.status('new'))
        worker.enqueue(self.db, [revenue()], NOW + 3)
        self.assertEqual(self.status('new'), 'pending')

    def test_production_read_only_source(self):
        source = Path(self.tmp.name) / 'source.db'
        with sqlite3.connect(source) as db:
            db.execute('''CREATE TABLE appstorerevenueevent (id TEXT, product_id TEXT,
              purchase_date_ms INTEGER, expires_date_ms INTEGER, price_milliunits INTEGER,
              currency TEXT, starts_trial INTEGER, transaction_reason TEXT, revoked_date_ms INTEGER,
              purchase_environment TEXT)''')
            for env in ('Production', 'Sandbox', 'Xcode'):
                row = revenue(env)
                db.execute('INSERT INTO appstorerevenueevent VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (*row.values(), env))
        before = source.read_bytes()
        self.assertEqual([r['id'] for r in worker.read_revenue(source)], ['Production'])
        self.assertEqual(before, source.read_bytes())

    def test_initialize_cannot_reset_state(self):
        with self.assertRaises(ValueError):
            worker.initialize(self.db, [], NOW + 2)

    def test_enqueue_requires_initialization(self):
        with worker.open_state(':memory:') as db:
            with self.assertRaises(ValueError):
                worker.enqueue(db, [revenue()], NOW + 2)

    def test_atomic_enqueue_failure_rolls_back(self):
        invalid = revenue('invalid', product_id=None)
        with self.assertRaises(AttributeError):
            worker.enqueue(self.db, [revenue(), invalid], NOW + 2)
        self.assertIsNone(self.status('new'))
        self.assertIsNone(self.db.execute("SELECT 1 FROM seen WHERE transaction_id='new'").fetchone())

    def test_retry_only_explicit_rejection(self):
        rows = [revenue()]
        worker.enqueue(self.db, rows, NOW + 2)
        worker.dispatch(self.db, rows, lambda _: self.reject(), NOW + 2)
        self.assertEqual(self.status('new'), 'pending')
        sent = []
        worker.dispatch(self.db, rows, lambda text: sent.append(text) or 2, NOW + 1000)
        self.assertFalse(sent)
        worker.dispatch(self.db, rows, lambda text: sent.append(text) or 2, NOW + 60002)
        self.assertEqual(len(sent), 1)

    @staticmethod
    def reject():
        raise worker.Rejected(60)

    def test_ambiguous_delivery_not_retried(self):
        rows = [revenue()]
        worker.enqueue(self.db, rows, NOW + 2)
        with patch.object(worker, 'telegram_send', side_effect=TimeoutError('secret URL')) as send:
            worker.dispatch(self.db, rows, send, NOW + 2)
            worker.dispatch(self.db, rows, send, NOW + 999999)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.status('new'), 'unknown')

    def test_crash_mid_send_held_after_reopening(self):
        worker.enqueue(self.db, [revenue()], NOW + 2)
        with self.db:
            self.db.execute("UPDATE alerts SET status='sending' WHERE id='new'")
        with worker.open_state(self.state) as reopened:
            worker.dispatch(reopened, [revenue()], lambda _: self.fail('must not resend'), NOW + 3)
        self.assertEqual(self.status('new'), 'unknown')

    def test_revocation_before_send_suppressed(self):
        worker.enqueue(self.db, [revenue()], NOW + 2)
        worker.dispatch(self.db, [revenue(revoked_date_ms=NOW + 3)],
                        lambda _: self.fail('must not send'), NOW + 4)
        self.assertEqual(self.status('new'), 'suppressed')

    def test_future_effective_payment_and_privacy(self):
        row = revenue('private-transaction-id', price_milliunits=1500000, currency='JPY',
                      product_id='sofly.weekly', transaction_reason='RENEWAL')
        text = worker.format_message(row, NOW)
        self.assertIn('Weekly', text)
        self.assertIn('JPY 1500', text)
        self.assertIn('Type: renewal', text)
        self.assertIn('has not started yet', text)
        self.assertNotIn(row['id'], text)
        self.assertNotIn('has not started yet', worker.format_message(row, NOW + 2))

    def test_missing_state_fails_closed(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--db', '/nonexistent-source',
                                 '--state', str(Path(self.tmp.name) / 'missing.db')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((Path(self.tmp.name) / 'missing.db').exists())

    def test_lock_prevents_concurrent_delivery(self):
        with open(self.state + '.lock', 'a') as lock:
            worker.fcntl.flock(lock, worker.fcntl.LOCK_EX)
            result = subprocess.run([sys.executable, str(SCRIPT), '--db', '/unused',
                                     '--state', self.state], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {'busy': True})


class TelegramTransportTests(unittest.TestCase):
    def response(self, payload):
        return io.BytesIO(json.dumps(payload).encode())

    def test_acknowledged_send(self):
        with patch.object(worker.request, 'urlopen', return_value=self.response({'ok': True, 'result': {'message_id': 42}})) as call:
            self.assertEqual(worker.telegram_send('test', 'secret', 'channel'), 42)
        request = call.call_args.args[0]
        self.assertEqual(json.loads(request.data)['text'], 'test')

    def test_explicit_rejection(self):
        with patch.object(worker.request, 'urlopen', return_value=self.response({'ok': False, 'parameters': {'retry_after': 99}})):
            with self.assertRaises(worker.Rejected) as caught:
                worker.telegram_send('test', 'secret', 'channel')
        self.assertEqual(caught.exception.retry_after, 99)

    def test_http_rejection_is_retryable(self):
        failure = HTTPError('secret-url', 429, 'rejected', {}, self.response({'ok': False}))
        with patch.object(worker.request, 'urlopen', side_effect=failure):
            with self.assertRaises(worker.Rejected):
                worker.telegram_send('test', 'secret', 'channel')

    def test_timeout_redacts_exception(self):
        with patch.object(worker.request, 'urlopen', side_effect=TimeoutError('secret-token')):
            with self.assertRaises(worker.Ambiguous) as caught:
                worker.telegram_send('test', 'secret', 'channel')
        self.assertEqual(str(caught.exception), '')

    def test_invalid_response_not_retryable(self):
        for payload in ({'ok': True}, {'ok': True, 'result': {'message_id': 'not-integer'}}):
            with patch.object(worker.request, 'urlopen', return_value=self.response(payload)):
                with self.assertRaises(worker.Ambiguous):
                    worker.telegram_send('test', 'secret', 'channel')


if __name__ == '__main__':
    unittest.main()
