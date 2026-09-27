"""HTTP boundary tests, with legacy model initialization confined to scratch."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

_import_scratch = tempfile.TemporaryDirectory(prefix='sofly-cockpit-api-')
_previous_cwd = os.getcwd()
try:
    os.chdir(_import_scratch.name)
    from tests import test_experiment_reporting as _environment
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.routers.cockpit import router, get_current_user
    from core.services.cockpit_stories import save_messages, utc_string
finally:
    os.chdir(_previous_cwd)

class CockpitAPITests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='sofly-cockpit-wire-')
        self.path = str(Path(self.scratch.name)/'cache.sqlite')
        self.env = patch.dict(os.environ, {'SOFLY_COCKPIT_DB': self.path})
        self.env.start()
        self.app = FastAPI()
        self.app.include_router(router, prefix='/cockpit')
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.scratch.cleanup()

    def test_unauthenticated_read_rejected(self):
        self.assertIn(self.client.get('/cockpit/stories').status_code, (401,403))

    def test_unconfigured_and_stale_not_misreported_as_empty(self):
        self.app.dependency_overrides[get_current_user] = lambda: object()
        self.assertEqual(self.client.get('/cockpit/stories').status_code,503)
        old=datetime.now(timezone.utc)-timedelta(hours=3)
        save_messages(self.path,[],old)
        self.assertEqual(self.client.get('/cockpit/stories').status_code,503)

    def test_success_empty_and_exact_filter(self):
        self.app.dependency_overrides[get_current_user] = lambda: object()
        now=datetime.now(timezone.utc)
        save_messages(self.path,[dict(timestamp=utc_string(now),tail='OE-TEST',flightNumber='EC123',text='NEED GPU UPON ARRIVAL APU INOP')],now)
        response=self.client.get('/cockpit/stories?registration=oe-test')
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json()['stories']),1)
        self.assertEqual(self.client.get('/cockpit/stories?registration=OE-OTHER').json()['stories'],[])
        self.assertEqual(self.client.get('/cockpit/stories?registration=%27%20OR%201=1').status_code,422)

    def test_corrupt_cache_does_not_leak_sql_or_traceback(self):
        self.app.dependency_overrides[get_current_user] = lambda: object()
        Path(self.path).write_text('not a database')
        response=self.client.get('/cockpit/stories')
        self.assertEqual(response.status_code,503)
        self.assertNotIn(self.path,response.text)

if __name__ == '__main__': unittest.main()
