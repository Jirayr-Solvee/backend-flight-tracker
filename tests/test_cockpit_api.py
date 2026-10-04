"""HTTP boundary tests, with legacy model initialization confined to scratch."""
import json
import os
import tempfile
from contextlib import closing
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_import_scratch = tempfile.TemporaryDirectory(prefix='sofly-cockpit-api-')
_previous_cwd = os.getcwd()
try:
    os.chdir(_import_scratch.name)
    from tests import test_experiment_reporting as _environment
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.routers.cockpit import router, get_current_user
    from core.services.cockpit_stories import open_store, save_messages, utc_string
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
        self.assertIn(self.client.get('/cockpit/stories/'+'a'*24).status_code, (401,403))

    def test_exact_story_detail_and_missing_message(self):
        self.app.dependency_overrides[get_current_user] = lambda: object()
        now=datetime.now(timezone.utc)
        save_messages(self.path,[dict(timestamp=utc_string(now),tail='OE-TEST',flightNumber='EC123',text='NEED GPU UPON ARRIVAL APU INOP')],now)
        story=self.client.get('/cockpit/stories').json()['stories'][0]
        self.assertEqual(self.client.get('/cockpit/stories/'+story['id']).json()['id'],story['id'])
        self.assertEqual(self.client.get('/cockpit/stories/'+'b'*24).status_code,404)
        self.assertEqual(self.client.get('/cockpit/stories/not-an-id').status_code,404)

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

    def test_top_sort_ranks_importance_and_default_order_is_unchanged(self):
        self.app.dependency_overrides[get_current_user] = lambda: object()
        now=datetime.now(timezone.utc)
        stories=[('a'*24,'Ground power requested','NEED GPU UPON ARRIVAL',60,1),
                 ('b'*24,'Holding over Frankfurt','HOLDING DUE TRAFFIC',90,2),
                 ('c'*24,'Smoke in the cabin','SMOKE IN AFT GALLEY',65,3),
                 ('d'*24,'Earlier smoke report','SMOKE IN FWD GALLEY',99,80*60)]
        with closing(open_store(self.path)) as db, db:
            db.execute("INSERT INTO cockpit_metadata VALUES ('updated_at',?)",(utc_string(now),))
            for key,title,text,interest,minutes in stories:
                received=utc_string(now-timedelta(minutes=minutes))
                registration='N'+key[:3].upper()
                story=dict(id=key,title=title,transmission=text,interestScore=interest,receivedAt=received,registration=registration)
                db.execute('INSERT INTO cockpit_stories VALUES (?,?,?,?)',(key,received,registration,json.dumps(story)))
        # Older app versions: same newest-first page, with an additional tier field.
        latest=self.client.get('/cockpit/stories').json()
        self.assertEqual([story['id'] for story in latest['stories']],['a'*24,'b'*24,'c'*24,'d'*24])
        self.assertEqual([story['tier'] for story in latest['stories']],['background','notable','major','major'])
        # A major event outranks a higher-scored notable one; background and >72 h stay out.
        top=self.client.get('/cockpit/stories?sort=top').json()
        self.assertEqual([story['id'] for story in top['stories']],['c'*24,'b'*24])
        self.assertIsNone(top['nextCursor'])
        self.assertEqual(self.client.get('/cockpit/stories/'+'b'*24).json()['tier'],'notable')
        self.assertEqual(self.client.get('/cockpit/stories?sort=popular').status_code,422)

    def test_corrupt_cache_does_not_leak_sql_or_traceback(self):
        self.app.dependency_overrides[get_current_user] = lambda: object()
        Path(self.path).write_text('not a database')
        response=self.client.get('/cockpit/stories')
        self.assertEqual(response.status_code,503)
        self.assertNotIn(self.path,response.text)

    def test_flight_scope_requires_ownership_and_handles_missing_assignment(self):
        self.assertIn(self.client.get('/cockpit/flights/1/stories').status_code,(401,403))
        self.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='owner')
        with patch('core.routers.cockpit.load_targets',return_value=(False,[])) as lookup:
            self.assertEqual(self.client.get('/cockpit/flights/1/stories').status_code,404)
            self.assertEqual(lookup.call_args.kwargs,{'user_id':'owner','flight_id':1})
        with patch('core.routers.cockpit.load_targets',return_value=(True,[])):
            response=self.client.get('/cockpit/flights/1/stories')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['coverage'],'awaiting_aircraft_or_schedule')

    def test_flight_scope_passed_to_reader_and_storage_errors_fail_closed(self):
        self.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='owner')
        with patch('core.routers.cockpit.load_targets',return_value=(True,[{'id':1}])):
            with patch('core.routers.cockpit.read_stories',return_value={'stories':[]}) as reader:
                self.assertEqual(self.client.get('/cockpit/flights/1/stories').status_code,200)
                self.assertEqual(reader.call_args.kwargs,{'flight':{'id':1},'limit':20,'category':None,'cursor':None})
        with patch('core.routers.cockpit.load_targets',side_effect=OSError('private path')):
            response=self.client.get('/cockpit/flights/1/stories')
            self.assertEqual(response.status_code,503)
            self.assertNotIn('private path',response.text)

    def test_pagination_parameters_are_validated(self):
        self.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='owner')
        for query in ('limit=0','limit=51','category=Unknown','cursor=not-a-cursor'):
            self.assertEqual(self.client.get('/cockpit/stories?'+query).status_code,422)
            self.assertEqual(self.client.get('/cockpit/flights/1/stories?'+query).status_code,422)

if __name__ == '__main__': unittest.main()
