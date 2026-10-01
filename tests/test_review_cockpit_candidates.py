import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from core.services.cockpit_ingestion import init_store
from core.services.cockpit_stories import utc_string
from scripts import review_cockpit_candidates as review


class ReviewCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.db_path=str(Path(self.directory.name)/'stories.sqlite')
        self.key_path=str(Path(self.directory.name)/'provider.env')
        Path(self.key_path).write_text('AIRFRAMES_API_KEY=fake-test-key\n')
        with contextlib.closing(init_store(self.db_path)) as db:
            with db:
                db.execute("INSERT INTO cockpit_review VALUES (?,?,?,?,?,?)",
                           ('a'*24,utc_string(datetime.now(timezone.utc)),'N123AB',42,'sensitive_event','pending'))
    def tearDown(self):self.directory.cleanup()
    def invoke(self,*arguments):
        output=io.StringIO()
        with patch.object(sys,'argv',['review_cockpit_candidates.py','--db',self.db_path,
                                      '--key-file',self.key_path,*arguments]),contextlib.redirect_stdout(output):
            review.main()
        return output.getvalue()
    def test_list_does_not_fetch_or_show_raw_text(self):
        with patch.object(review.httpx,'get',side_effect=AssertionError('unexpected fetch')):
            output=self.invoke()
        self.assertIn('sensitive_event',output)
        self.assertNotIn('MAYDAY',output)
    def test_show_redacts_contact_and_checks_provider_identity(self):
        payload={'id':42,'tail':'N123AB','text':'MAYDAY call +1 202 555 0199 email me@example.com'}
        with patch.object(review.httpx,'get',return_value=httpx.Response(200,json=payload,request=httpx.Request('GET','https://api.airframes.io/v1/messages/42'))):
            output=self.invoke('--show','a'*24)
        self.assertIn('MAYDAY',output)
        self.assertNotIn('555',output)
        self.assertNotIn('me@example.com',output)
        payload['tail']='WRONG'
        with patch.object(review.httpx,'get',return_value=httpx.Response(200,json=payload,request=httpx.Request('GET','https://api.airframes.io/v1/messages/42'))):
            with self.assertRaises(SystemExit):self.invoke('--show','a'*24)
    def test_manual_disposition_never_publishes(self):
        self.assertEqual(self.invoke('--mark','a'*24,'--decision','dismissed').strip(),'Updated')
        with contextlib.closing(init_store(self.db_path)) as db:
            self.assertEqual(db.execute('SELECT status FROM cockpit_review').fetchone()[0],'dismissed')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cockpit_stories').fetchone()[0],0)


if __name__=='__main__':unittest.main()
