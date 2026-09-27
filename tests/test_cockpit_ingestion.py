import asyncio
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import httpx
from core.services import cockpit_ingestion as c

class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.tmp.name)/'feed.sqlite')
        self.db=c.init_store(self.path)
        self.now=datetime.now(timezone.utc)-timedelta(minutes=5)
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def row(self):
        return dict(id=100,timestamp=c.utc_string(self.now),createdAt=c.utc_string(self.now),tail='N123AB',flightNumber='AA123',text='NEED GPU UPON ARRIVAL APU INOP')
    def test_atomic_limit_and_rollback(self):
        self.assertTrue(c.reserve(self.db,[('a',1,1),('b',1,5)]))
        self.assertFalse(c.reserve(self.db,[('b',1,5),('a',1,1)]))
        self.assertEqual(self.db.execute("SELECT used FROM cockpit_budget WHERE bucket='b'").fetchone()[0],1)
    def test_concurrent_reservation(self):
        def worker(_):
            db=c.init_store(self.path)
            try:return c.reserve(db,[('concurrent',1,5)])
            finally:db.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(worker,range(20))),5)
    def test_ai_month_ceiling_survives_restart_and_day_change(self):
        date=datetime(2026,9,1,tzinfo=timezone.utc)
        self.db.execute('INSERT INTO cockpit_budget VALUES (?,?)',('ai-month:2026-09',179.99));self.db.commit()
        self.assertTrue(c.ai_allowance(self.db,date))
        other=c.init_store(self.path)
        try:self.assertFalse(c.ai_allowance(other,date+timedelta(days=1)))
        finally:other.close()
    def test_daily_cap(self):
        date=datetime(2026,9,1,tzinfo=timezone.utc)
        self.db.execute('INSERT INTO cockpit_budget VALUES (?,?)',('ai-day:2026-09-01',6));self.db.commit()
        self.assertFalse(c.ai_allowance(self.db,date))
        self.assertTrue(c.ai_allowance(self.db,date+timedelta(days=1)))
    def test_queue_dedupe_and_private(self):
        with self.db:
            c.queue_message(self.db,self.row(),datetime.now(timezone.utc))
            c.queue_message(self.db,self.row(),datetime.now(timezone.utc))
            c.queue_message(self.db,dict(self.row(),text='PLEASE EMAIL me@example.com'),datetime.now(timezone.utc))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_queue').fetchone()[0],1)
    def test_publication_guards(self):
        msg=dict(id='x',text='NEED GPU UPON ARRIVAL APU INOP',flight=None,registration='N123AB',receivedAt=c.utc_string(self.now))
        good=dict(publish=True,needs_review=False,category='Operations',title='Ground power requested',summary='The crew requests external power.',excerpt='NEED GPU',interest=50)
        self.assertIsNotNone(c.validate_story(msg,good))
        for change in (dict(needs_review=True),dict(publish='true'),dict(excerpt='invented'),dict(interest=101),dict(summary='email me@example.com'),dict(category='Unknown')):
            self.assertIsNone(c.validate_story(msg,dict(good,**change)))
    def test_success_mock_and_no_duplicate_ai(self):
        calls=[]
        def handler(req):
            calls.append(req.method)
            if req.method=='GET':return httpx.Response(200,json=[self.row()])
            data=dict(publish=True,needs_review=False,category='Operations',title='Ground power',summary='The crew requests ground power.',excerpt='NEED GPU',interest=50)
            return httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(data)}]}}]})
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(c,'TERMS',('NEED GPU',)),patch.object(c.asyncio,'sleep',return_value=None):
                    first=await c.run(self.path,'air','gem',client)
                    # Reset only cursor in test so duplicate is returned in-window again.
                    self.db.execute('DELETE FROM cockpit_cursor');self.db.commit()
                    second=await c.run(self.path,'air','gem',client)
                    self.assertEqual(first['published'],1)
                    self.assertEqual(second['ai_requests'],0)
        asyncio.run(run())
        self.assertEqual(calls.count('POST'),1)
    def test_429_does_not_advance_or_call_ai(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(429))) as client:
                with patch.object(c,'TERMS',('x',)):
                    result=await c.run(self.path,'air','gem',client)
                    self.assertEqual(result['errors'],1);self.assertEqual(result['ai_requests'],0)
        asyncio.run(run())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_cursor').fetchone()[0],0)

if __name__=='__main__':unittest.main()
