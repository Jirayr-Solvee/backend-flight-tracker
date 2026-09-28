import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from core.services.story_push import eligible, slot_for, reserve, dispatch, payload
from core.services.cockpit_stories import open_store, utc_string


class StoryPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name)/'devices.sqlite')
        self.feed = str(Path(self.tmp.name)/'stories.sqlite')
        self.now = datetime(2026, 9, 28, 10, 10, tzinfo=timezone.utc)
        self.device = dict(device_id='phone', user_id='owner', owner_id='owner',
            app_version='3.9.3', build_number=142, capability=1, enabled=1,
            apn_token_active=1, apn_token='test-token', environment='production',
            updated_at=int(self.now.timestamp()), time_zone='UTC', language='en')
        self.db = sqlite3.connect(self.database)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE storypushdelivery(id TEXT PRIMARY KEY,user_id TEXT,device_id TEXT,story_id TEXT,story_fingerprint TEXT,reserved_at INTEGER,local_day TEXT,slot INTEGER,status TEXT);
          CREATE TABLE device(id TEXT PRIMARY KEY,user_id TEXT,apn_token TEXT,apn_token_active INTEGER);
          CREATE TABLE storypushdevice(device_id TEXT PRIMARY KEY,user_id TEXT,app_version TEXT,build_number INTEGER,capability INTEGER,enabled INTEGER,language TEXT,time_zone TEXT,environment TEXT,updated_at INTEGER);
        ''')
        self.db.execute('INSERT INTO device VALUES (?,?,?,?)', ('phone','owner','test-token',1))
        self.db.execute('INSERT INTO storypushdevice VALUES (?,?,?,?,?,?,?,?,?,?)',
            tuple(self.device[k] for k in ('device_id','user_id','app_version','build_number','capability','enabled','language','time_zone','environment','updated_at')))
        self.db.commit()
        self.stories = [dict(id=f'{i:024x}', title=f'Title {i}', summary='A supported explanation.',
            transmission=f'MESSAGE {i}', receivedAt=utc_string(self.now-timedelta(minutes=10)),
            registration='N123', interestScore=90-i, notificationEligible=True,
            translations={'fr':dict(title='Un message',summary='Une explication.')} ) for i in range(6)]
        with open_store(self.feed) as db:
            db.execute("INSERT INTO cockpit_metadata VALUES ('updated_at',?)",(utc_string(self.now),))
            for s in self.stories:
                db.execute('INSERT INTO cockpit_stories VALUES (?,?,?,?)',(s['id'],s['receivedAt'],s['registration'],json.dumps(s)))

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def test_exact_version_and_capability_fail_closed(self):
        self.assertTrue(eligible(self.device,self.now,'3.9.3',142))
        for key,value in [('build_number',141),('build_number',143),('app_version','3.9.2'),
                          ('capability',0),('enabled',0),('apn_token_active',0),('apn_token',''),
                          ('owner_id','other'),('environment','testflight'),('environment','development'),
                          ('updated_at',0)]:
            with self.subTest(key=key,value=value):
                self.assertFalse(eligible(dict(self.device,**{key:value}),self.now,'3.9.3',142))
        self.assertFalse(eligible(self.device,self.now,'',0))

    def test_daytime_slots_and_timezone(self):
        self.assertEqual(slot_for(self.now,'UTC'),('2026-09-28',0))
        self.assertIsNone(slot_for(self.now.replace(hour=23),'UTC')[1])
        self.assertEqual(slot_for(self.now.replace(hour=6),'Asia/Yerevan')[1],0)

    def test_three_per_day_spacing_and_no_repeat_across_devices(self):
        first=reserve(self.db,self.device,self.stories,self.now)
        self.assertIsNotNone(first)
        self.assertIsNone(reserve(self.db,dict(self.device,device_id='second'),self.stories,self.now))
        second=reserve(self.db,self.device,self.stories,self.now.replace(hour=15))
        third=reserve(self.db,self.device,self.stories,self.now.replace(hour=19))
        self.assertEqual(len({r[1]['id'] for r in (first,second,third)}),3)
        self.assertIsNone(reserve(self.db,self.device,self.stories,self.now.replace(hour=20)))
        self.assertIsNone(reserve(self.db,self.device,self.stories,self.now+timedelta(days=1,minutes=-1)))
        fourth=reserve(self.db,self.device,self.stories,self.now+timedelta(days=1,minutes=1))
        self.assertNotIn(fourth[1]['id'],{r[1]['id'] for r in (first,second,third)})

    def test_independent_worker_cannot_duplicate_reservation(self):
        other=sqlite3.connect(self.database); other.row_factory=sqlite3.Row
        try:
            self.assertIsNotNone(reserve(self.db,self.device,self.stories,self.now))
            self.assertIsNone(reserve(other,self.device,self.stories,self.now))
        finally: other.close()

    def test_payload_uses_translation_and_no_fake_flight(self):
        value=payload(self.stories[0],'fr','notification')
        self.assertEqual(value['aps']['alert']['title'],'Un message')
        self.assertNotIn('flight_id',value)
        self.assertIsNone(payload(self.stories[0],'ar','notification'))

    def test_disabled_never_opens_database_or_calls_sender(self):
        async def send(*args): self.fail('must not send')
        result=asyncio.run(dispatch('/missing','/missing',send))
        self.assertEqual(result['reserved'],0)

    def test_dispatch_success_and_uncertain_send_never_retried(self):
        calls=[]
        async def send(*args): calls.append(args); return '200'
        result=asyncio.run(dispatch(self.database,self.feed,send,enabled=True,version='3.9.3',build=142,now=self.now))
        self.assertEqual(result['accepted'],1)
        asyncio.run(dispatch(self.database,self.feed,send,enabled=True,version='3.9.3',build=142,now=self.now))
        self.assertEqual(len(calls),1)
        self.db.execute('DELETE FROM storypushdelivery'); self.db.commit()
        async def uncertain(*args): calls.append(args); raise TimeoutError()
        asyncio.run(dispatch(self.database,self.feed,uncertain,enabled=True,version='3.9.3',build=142,now=self.now))
        asyncio.run(dispatch(self.database,self.feed,uncertain,enabled=True,version='3.9.3',build=142,now=self.now))
        self.assertEqual(len(calls),2)
        self.assertEqual(self.db.execute('SELECT status FROM storypushdelivery').fetchone()[0],'unknown')

    def test_stale_and_unqualified_stories_do_not_fill_quota(self):
        async def send(*args): self.fail('must not send')
        with sqlite3.connect(self.feed) as db:
            db.execute("UPDATE cockpit_metadata SET value=?",(utc_string(self.now-timedelta(hours=3)),))
        result=asyncio.run(dispatch(self.database,self.feed,send,enabled=True,version='3.9.3',build=142,now=self.now))
        self.assertEqual(result['reserved'],0)


if __name__ == '__main__': unittest.main()
