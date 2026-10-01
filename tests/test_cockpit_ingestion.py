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
        self.db.execute('INSERT INTO cockpit_budget VALUES (?,?)',('ai-month:2026-09',c.MONTHLY_USD-c.RESERVE_USD));self.db.commit()
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
    def test_bulletins_are_not_aircraft_events(self):
        for text in ('LEMH ATIS BIRD STRIKE WARNING IN VICINITY',
                     'BIRD STRIKE RISK WARNING FOR ARRIVAL ROUTE',
                     'SIGMET MODERATE TURBULENCE EXPECTED',
                     'MSG FROM GND LOADCONTROL: FUEL FIGURES RECEIVED. THANK YOU',
                     'LIVE ANIMALS ONBOARD TEMPERATURE CONTROL IN AFT COMPARTMENT'):
            self.assertTrue(c.routine_message(text),text)
            with self.db:c.queue_message(self.db,dict(self.row(),text=text),datetime.now(timezone.utc))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_queue').fetchone()[0],0)
        self.assertFalse(c.routine_message('DIVERTING TO KIAH DUE SIGMET AT DESTINATION'))
        self.assertFalse(c.routine_message('BIRD STRIKE ON CLIMB OUT RETURNING TO KIAH'))
        self.assertFalse(c.routine_message('ITS OUR FIRST OFFICERS BIRTHDAY TODAY'))
    def test_sensitive_event_has_only_private_metadata(self):
        row=dict(self.row(),text='MAYDAY DIVERTING TO KIAH PLEASE PHONE 12345678901')
        with self.db:c.queue_message(self.db,row,datetime.now(timezone.utc))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_queue').fetchone()[0],0)
        record=self.db.execute('SELECT provider_id,reason FROM cockpit_review').fetchone()
        self.assertEqual(record,(100,'sensitive_event'))
        self.assertNotIn('MAYDAY',str(self.db.execute('SELECT * FROM cockpit_review').fetchall()))
    def test_exact_global_duplicate_keeps_tracked_copy(self):
        first=self.row();second=dict(first,tail='N456CD')
        with self.db:
            c.queue_message(self.db,first,datetime.now(timezone.utc))
            c.queue_message(self.db,second,datetime.now(timezone.utc))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_queue').fetchone()[0],1)
        with self.db:c.queue_message(self.db,second,datetime.now(timezone.utc),tracked=True)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_queue').fetchone()[0],2)
    def test_retire_legacy_bulletin_backlog(self):
        legacy=dict(id='a'*24,registration='N123AB',receivedAt=c.utc_string(self.now),text='ATIS BIRD STRIKE WARNING')
        with self.db:self.db.execute('INSERT INTO cockpit_queue VALUES (?,?,?,?)',
                                     (legacy['id'],legacy['receivedAt'],json.dumps(legacy),'pending'))
        self.assertEqual(c.suppress_routine_pending(self.db),1)
        self.assertEqual(self.db.execute('SELECT status,payload FROM cockpit_queue').fetchone(),('suppressed','{}'))
    def test_retire_previously_published_bulletin(self):
        old={'id':'a'*24,'transmission':'ATIS BIRD STRIKE WARNING IN VICINITY'}
        real={'id':'b'*24,'transmission':'BIRD STRIKE ON CLIMB OUT RETURNING TO KIAH'}
        with self.db:
            for story in (old,real):
                self.db.execute('INSERT INTO cockpit_stories VALUES (?,?,?,?)',
                                (story['id'],c.utc_string(self.now),'N123AB',json.dumps(story)))
                self.db.execute('INSERT INTO cockpit_queue VALUES (?,?,?,?)',
                                (story['id'],c.utc_string(self.now),'{}','published'))
        self.assertEqual(c.retire_routine_stories(self.db),1)
        self.assertEqual(c.retire_routine_stories(self.db),0)
        self.assertEqual(self.db.execute('SELECT id FROM cockpit_stories').fetchone()[0],real['id'])
        self.assertEqual(self.db.execute("SELECT status FROM cockpit_queue WHERE id=?",(old['id'],)).fetchone()[0],'suppressed')
    def test_publication_guards(self):
        msg=dict(id='x',text='NEED GPU UPON ARRIVAL APU INOP',flight=None,registration='N123AB',receivedAt=c.utc_string(self.now))
        good=dict(publish=True,needs_review=False,category='Operations',title='Ground power requested',summary='The crew requests external power.',excerpt='NEED GPU',interest=50,translations={lang:{'title':'Ground power','summary':'External power requested.'} for lang in c.LANGUAGES})
        self.assertIsNotNone(c.validate_story(msg,good))
        for change in (dict(translations={}),dict(translations=None),dict(needs_review=True),dict(publish='true'),dict(excerpt='invented'),dict(interest=101),dict(summary='email me@example.com'),dict(category='Unknown')):
            self.assertIsNone(c.validate_story(msg,dict(good,**change)))
    def test_success_mock_and_no_duplicate_ai(self):
        calls=[]
        def handler(req):
            calls.append(req.method)
            if req.method=='GET':return httpx.Response(200,json=[self.row()])
            data=dict(publish=True,needs_review=False,category='Operations',title='Ground power',summary='The crew requests ground power.',excerpt='NEED GPU',interest=50,translations={lang:{'title':'Ground power','summary':'External power requested.'} for lang in c.LANGUAGES})
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
    def test_translation_contract_and_budget_bound(self):
        translations={lang:{'title':'A title','summary':'An explanation.'} for lang in c.LANGUAGES}
        self.assertTrue(c.valid_translations(translations))
        for invalid in ({},dict(translations,xx=translations['ar']),dict(translations,ar={'title':'','summary':'x'}),
                        dict(translations,ar={'title':'x','summary':'email me@example.com'}),
                        dict(translations,ar={'title':'x'*181,'summary':'x'})):
            self.assertFalse(c.valid_translations(invalid))
        self.assertGreaterEqual(c.RESERVE_USD,10000*.30/1_000_000+6144*2.50/1_000_000)
        self.assertLessEqual(c.MONTHLY_USD,180)
    def test_429_does_not_advance_or_call_ai(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(429))) as client:
                with patch.object(c,'TERMS',('x',)):
                    result=await c.run(self.path,'air','gem',client)
                    self.assertEqual(result['errors'],1);self.assertEqual(result['ai_requests'],0)
        asyncio.run(run())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_cursor').fetchone()[0],0)
    def test_theme_fairness(self):
        with self.db:
            for i in range(25):c.queue_message(self.db,dict(self.row(),text=f'NEED GPU UPON ARRIVAL APU INOP {i}'),datetime.now(timezone.utc))
            c.queue_message(self.db,dict(self.row(),text='ITS OUR FIRST OFFICERS BIRTHDAY TODAY'),datetime.now(timezone.utc))
        chosen=c.pending_messages(self.db)
        self.assertEqual(len(chosen),20)
        self.assertTrue(any('BIRTHDAY' in json.loads(payload)['text'] for _,payload in chosen))
    def test_specific_action_precedes_weather_and_acknowledgement(self):
        with self.db:
            c.queue_message(self.db,dict(self.row(),tail='N1ABC',text='DEVIATING FOR TURBULENCE'),datetime.now(timezone.utc))
            c.queue_message(self.db,dict(self.row(),tail='N2ABC',text='DIVERTING TO KIAH DUE FUEL'),datetime.now(timezone.utc))
            c.queue_message(self.db,dict(self.row(),tail='N3ABC',text='ITS OUR FIRST OFFICERS BIRTHDAY TODAY'),datetime.now(timezone.utc))
        chosen=c.pending_messages(self.db)
        self.assertIn('DIVERTING TO KIAH',json.loads(chosen[0][1])['text'])
        self.assertEqual(len(chosen),3)
    def test_model_review_has_no_public_story_or_raw_queue_payload(self):
        def handler(req):
            if req.method=='GET':return httpx.Response(200,json=[dict(self.row(),text='SMELL IN CABIN REPORTED')])
            item=dict(publish=False,needs_review=True,category='Cabin',title='Review',summary='Review needed',
                      excerpt='SMELL',interest=80,translations={lang:{'title':'Review','summary':'Review needed'} for lang in c.LANGUAGES})
            return httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(item)}]}}]})
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(c,'TERMS',('SMELL',)),patch.object(c.asyncio,'sleep',return_value=None):
                    return await c.run(self.path,'air','gem',client)
        result=asyncio.run(run())
        self.assertEqual(result['review'],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_stories').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT status,payload FROM cockpit_queue').fetchone(),('review','{}'))
    def test_spending_is_paced_and_persistent(self):
        now=datetime(2026,9,1,12,0,tzinfo=timezone.utc)
        self.assertTrue(c.ai_allowance(self.db,now))
        self.assertTrue(c.ai_allowance(self.db,now))
        self.assertFalse(c.ai_allowance(self.db,now+timedelta(minutes=9)))
        self.assertTrue(c.ai_allowance(self.db,now+timedelta(minutes=10)))
    def test_incomplete_ai_not_retried(self):
        def handler(req):
            if req.method=='GET':return httpx.Response(200,json=[self.row()])
            return httpx.Response(200,json={'candidates':[{'finishReason':'MAX_TOKENS'}]})
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(c,'TERMS',('NEED GPU',)),patch.object(c.asyncio,'sleep',return_value=None):
                    result=await c.run(self.path,'air','gem',client)
                    self.assertEqual(result['errors'],1)
        asyncio.run(run())
        self.assertEqual(self.db.execute('SELECT status FROM cockpit_queue').fetchone()[0],'failed')
        self.assertEqual(len(c.pending_messages(self.db)),0)
        self.assertIsNotNone(self.db.execute("SELECT value FROM cockpit_metadata WHERE key='updated_at'").fetchone())
    def test_full_pages_persist_cursor(self):
        def handler(req):
            first='before_id' not in req.url.params
            return httpx.Response(200,json=[dict(self.row(),id=i) for i in range(101,201) if first] if first else [dict(self.row(),id=i) for i in range(1,101)])
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(c,'TERMS',('NEED GPU',)),patch.object(c,'MAX_AI_PER_RUN',0),patch.object(c.asyncio,'sleep',return_value=None):
                    result=await c.run(self.path,'air','gem',client)
                    self.assertEqual(result['backlogged_terms'],1)
                    self.assertEqual(result['provider_requests'],2)
        asyncio.run(run())
        self.assertEqual(self.db.execute('SELECT before_id FROM cockpit_cursor').fetchone()[0],1)

if __name__=='__main__':unittest.main()
