import asyncio
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from core.services import cockpit_ai as ai,cockpit_ingestion as c


class CockpitAITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'feed.sqlite')
        self.db=c.init_store(self.path);self.now=datetime.now(timezone.utc)

    def tearDown(self):self.db.close();self.tmp.cleanup()

    def queue(self,text='NEED GPU UPON ARRIVAL APU INOP',reg='N123AB',hours=0):
        row=dict(id=42,timestamp=c.utc_string(self.now-timedelta(hours=hours)),tail=reg,flightNumber='AA123',text=text)
        with self.db:c.queue_message(self.db,row,self.now)
        return self.db.execute('SELECT id FROM cockpit_queue WHERE json_extract(payload,\'$.registration\')=?',(reg,)).fetchone()[0]

    def claim(self,key,now=None,db=None,kind='ground'):
        now=now or self.now
        return ai.claim(db or self.db,key,now,c.ai_budget_entries(now),kind,c.FRESH_CANDIDATE_HOURS)

    def http_fail(self,key,code,now=None,headers=None):
        return ai.http_failure(self.db,key,httpx.Response(code,headers=headers or {},text='SECRET body raw@example.com'),now or self.now,c.FRESH_CANDIDATE_HOURS)

    def test_independent_workers_claim_same_item_and_debit_only_once(self):
        key=self.queue()
        def attempt(_):
            db=c.init_store(self.path)
            try:return self.claim(key,db=db)
            finally:db.close()
        with ThreadPoolExecutor(max_workers=4) as pool:self.assertEqual(sum(pool.map(attempt,range(12))),1)
        self.assertEqual(self.db.execute('SELECT attempts FROM cockpit_ai_attempt').fetchone()[0],1)
        self.assertAlmostEqual(self.db.execute("SELECT used FROM cockpit_budget WHERE bucket LIKE 'ai-month:%'").fetchone()[0],.02)

    def test_different_items_share_atomic_two_per_window_cap(self):
        keys=[self.queue(reg=f'N{i}AB',text=f'NEED GPU UPON ARRIVAL {i}') for i in range(8)]
        def attempt(key):
            db=c.init_store(self.path)
            try:return self.claim(key,db=db)
            finally:db.close()
        with ThreadPoolExecutor(max_workers=4) as pool:self.assertEqual(sum(pool.map(attempt,keys)),2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_ai_attempt').fetchone()[0],2)

    def test_day_or_month_limit_never_claims_or_debits_any_bucket(self):
        key=self.queue()
        for bucket,amount,limit in c.ai_budget_entries(self.now)[:2]:
            with self.subTest(bucket=bucket):
                self.db.execute('DELETE FROM cockpit_budget')
                self.db.execute('INSERT INTO cockpit_budget VALUES (?,?)',(bucket,limit));self.db.commit()
                self.assertFalse(self.claim(key))
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_budget').fetchone()[0],1)
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_ai_attempt').fetchone()[0],0)
                self.assertEqual(self.db.execute('SELECT status FROM cockpit_queue').fetchone()[0],'pending')

    def test_interrupted_attempt_never_replayed_after_restart(self):
        key=self.queue();self.assertTrue(self.claim(key))
        other=c.init_store(self.path)
        try:self.assertFalse(self.claim(key,self.now+timedelta(hours=1),db=other))
        finally:other.close()
        self.assertEqual(c.pending_messages(self.db),[])

    def test_each_explicit_transient_defers_with_persistent_cooldown(self):
        for i,code in enumerate(sorted(ai.TRANSIENT_HTTP)):
            with self.subTest(code=code):
                self.db.execute('DELETE FROM cockpit_metadata');self.db.execute('DELETE FROM cockpit_budget');self.db.commit()
                key=self.queue(reg=f'N{i}ERR',text=f'NEED GPU {i}');self.assertTrue(self.claim(key))
                self.assertEqual(self.http_fail(key,code),('http_'+str(code),True))
                row=self.db.execute('SELECT status,payload FROM cockpit_queue WHERE id=?',(key,)).fetchone()
                self.assertEqual(row[0],'pending');self.assertNotEqual(row[1],'{}')
                self.assertTrue(ai.cooling_down(self.db,self.now))
                other=c.init_store(self.path)
                try:self.assertEqual(c.pending_messages(other,now=self.now),[])
                finally:other.close()

    def test_three_attempt_limit_each_charged_and_no_error_body_retained(self):
        key=self.queue();now=self.now
        for attempt in range(1,4):
            self.assertTrue(self.claim(key,now))
            reason,deferred=self.http_fail(key,503,now)
            self.assertEqual(deferred,attempt<3)
            if deferred:
                next_at=self.db.execute('SELECT next_attempt_at FROM cockpit_ai_attempt WHERE id=?',(key,)).fetchone()[0]
                now=c.parse_time(next_at)+timedelta(seconds=1)
        self.assertFalse(self.claim(key,now+timedelta(hours=1)))
        self.assertEqual(self.db.execute('SELECT status,payload FROM cockpit_queue').fetchone(),('failed','{}'))
        self.assertAlmostEqual(self.db.execute("SELECT used FROM cockpit_budget WHERE bucket LIKE 'ai-month:%'").fetchone()[0],.06)
        stored=str(self.db.execute('SELECT * FROM cockpit_ai_attempt').fetchall())
        self.assertNotIn('SECRET',stored);self.assertNotIn('@',stored)

    def test_permanent_and_ambiguous_http_never_retry(self):
        for i,code in enumerate((400,401,402,403,404,408,409,413,422,501,418)):
            with self.subTest(code=code):
                self.db.execute('DELETE FROM cockpit_metadata');self.db.execute('DELETE FROM cockpit_budget');self.db.commit()
                key=self.queue(reg=f'N{i}PER',text=f'NEED GPU {i}');self.assertTrue(self.claim(key))
                self.assertFalse(self.http_fail(key,code)[1])
                self.assertEqual(self.db.execute('SELECT status,payload FROM cockpit_queue WHERE id=?',(key,)).fetchone(),('failed','{}'))

    def test_retry_after_and_newer_cooldown_are_respected(self):
        key=self.queue();self.assertTrue(self.claim(key))
        newer=c.utc_string(self.now+timedelta(hours=3))
        self.db.execute("INSERT INTO cockpit_metadata VALUES ('ai-not-before',?)",(newer,));self.db.commit()
        self.http_fail(key,429,headers={'Retry-After':'3600'})
        self.assertEqual(self.db.execute("SELECT value FROM cockpit_metadata WHERE key='ai-not-before'").fetchone()[0],newer)
        next_at=c.parse_time(self.db.execute('SELECT next_attempt_at FROM cockpit_ai_attempt').fetchone()[0])
        self.assertGreaterEqual(next_at,self.now+timedelta(seconds=3599))
        for value in ('secret@example.com','-5','9'*5000):
            delay=ai.retry_delay(httpx.Response(503,headers={'Retry-After':value}),key,1,self.now)
            self.assertGreaterEqual(delay.total_seconds(),600);self.assertLessEqual(delay.total_seconds(),86400)

    def test_late_retry_never_extends_candidate_freshness(self):
        key=self.queue(hours=11.99);self.assertTrue(self.claim(key))
        self.assertFalse(self.http_fail(key,503)[1])
        self.assertEqual(self.db.execute('SELECT payload FROM cockpit_queue').fetchone()[0],'{}')

    def test_repeated_weather_holds_do_not_starve_other_topics(self):
        weather=self.queue('TURBULENCE REPORTED',reg='NWX1')
        cabin=self.queue('CATERING REQUEST',reg='NCAB')
        with self.db:
            for i in range(20):
                self.db.execute('INSERT INTO cockpit_ai_attempt VALUES (?,?,?,?,?,?,?)',
                    (f'{i:024x}',c.utc_string(self.now),'weather',1,c.utc_string(self.now),None,'review_unclear_meaning'))
        selected=c.pending_messages(self.db,now=self.now)
        self.assertEqual(selected[0][0],cabin)
        self.assertEqual({key for key,_ in selected},{weather,cabin})

    def test_missing_or_conflicting_review_reason_cannot_publish(self):
        message=dict(id='x',text='NEED GPU',flight=None,registration='N123AB',receivedAt=c.utc_string(self.now))
        item=dict(publish=True,needs_review=False,review_reason='none',category='Operations',title='Ground power',
                  summary='Power requested.',excerpt='NEED GPU',interest=50,
                  translations={lang:dict(title='Power',summary='Power requested.') for lang in c.LANGUAGES})
        self.assertIsNotNone(c.validate_story(message,item))
        for reason in (None,'safety','untrusted@example.com',{},['none']):
            with self.subTest(reason=reason):self.assertIsNone(c.validate_story(message,dict(item,review_reason=reason)))
        for reason in ('none','safety','untrusted@example.com',{}):
            reviewed=dict(item,needs_review=True,review_reason=reason)
            self.assertIsNone(c.validate_story(message,reviewed))
            self.assertIn(c.review_reason(reviewed),(*ai.REVIEW_REASONS,'unspecified'))

    def run_mock(self,post):
        def handler(request):
            if request.method=='GET':return httpx.Response(200,json=[])
            return post(request)
        async def execute():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(c,'TERMS',()),patch.object(c,'ROTATING_TERMS',()):
                    return await c.run(self.path,'fake-air','fake-gem',client)
        return asyncio.run(execute())

    def test_http_failure_stops_before_consuming_second_candidate(self):
        self.queue();self.queue('CATERING REQUEST',reg='NCAB')
        result=self.run_mock(lambda _:httpx.Response(503,text='SECRET api-key raw@example.com'))
        self.assertEqual((result['ai_requests'],result['ai_http_503'],result['ai_deferred']),(1,1,1))
        self.assertNotIn('SECRET',json.dumps(result));self.assertNotIn('@',json.dumps(result))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_ai_attempt').fetchone()[0],1)

    def test_timeout_is_unknown_terminal_and_never_replayed(self):
        key=self.queue()
        def timeout(request):raise httpx.ReadTimeout('SECRET key',request=request)
        result=self.run_mock(timeout)
        self.assertEqual(result['ai_timeout_unknown'],1)
        self.assertEqual(self.db.execute('SELECT status,payload FROM cockpit_queue WHERE id=?',(key,)).fetchone(),('failed','{}'))
        self.assertFalse(self.claim(key,self.now+timedelta(hours=1)))

    def test_review_reason_is_bounded_and_raw_payload_removed(self):
        self.queue('SMELL IN CABIN REPORTED')
        item=dict(publish=False,needs_review=True,review_reason='unclear_meaning')
        result=self.run_mock(lambda _:httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(item)}]}}]}))
        self.assertEqual(result['held_review_unclear_meaning'],1)
        self.assertEqual(self.db.execute('SELECT reason FROM cockpit_review').fetchone()[0],'model_review_unclear_meaning')
        self.assertEqual(self.db.execute('SELECT status,payload FROM cockpit_queue').fetchone(),('review','{}'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_stories').fetchone()[0],0)

    def test_attempt_metadata_retention_is_seven_days(self):
        with self.db:self.db.execute('INSERT INTO cockpit_ai_attempt VALUES (?,?,?,?,?,?,?)',
            ('old',c.utc_string(self.now-timedelta(days=8)),'weather',1,c.utc_string(self.now),None,'review_safety'))
        self.run_mock(lambda _:self.fail('unexpected AI request'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cockpit_ai_attempt').fetchone()[0],0)


if __name__=='__main__':unittest.main()
