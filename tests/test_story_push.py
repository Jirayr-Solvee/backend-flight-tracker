import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from core.services.story_push import eligible, slot_for, reserve, dispatch, payload, campaign_for, candidates
from core.services.cockpit_stories import open_store, utc_string
from core.services.cockpit_importance import with_tier


class StoryPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name)/'devices.sqlite')
        self.feed = str(Path(self.tmp.name)/'stories.sqlite')
        self.now = datetime(2026, 9, 28, 10, 10, tzinfo=timezone.utc)
        self.device = dict(device_id='phone', user_id='owner', owner_id='owner',
            app_version='3.9.3', build_number=142, capability=1, enabled=1,
            apn_token_active=1, apn_token='test-token', environment='production',
            updated_at=int(self.now.timestamp()), time_zone='UTC', language='en',
            premium_valid_until=int((self.now+timedelta(days=7)).timestamp()*1000))
        self.db = sqlite3.connect(self.database)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE storypushdelivery(id TEXT PRIMARY KEY,user_id TEXT,device_id TEXT,story_id TEXT,story_fingerprint TEXT,reserved_at INTEGER,local_day TEXT,slot INTEGER,status TEXT);
          CREATE TABLE storypushcampaign(day TEXT,slot INTEGER,story_id TEXT,story_fingerprint TEXT,category TEXT,created_at INTEGER,PRIMARY KEY(day,slot));
          CREATE TABLE user(id TEXT PRIMARY KEY,premium_valid_until INTEGER);
          CREATE TABLE device(id TEXT PRIMARY KEY,user_id TEXT,apn_token TEXT,apn_token_active INTEGER);
          CREATE TABLE storypushdevice(device_id TEXT PRIMARY KEY,user_id TEXT,app_version TEXT,build_number INTEGER,capability INTEGER,enabled INTEGER,language TEXT,time_zone TEXT,environment TEXT,updated_at INTEGER);
        ''')
        self.db.execute('INSERT INTO device VALUES (?,?,?,?)', ('phone','owner','test-token',1))
        self.db.execute('INSERT INTO user VALUES (?,?)',('owner',self.device['premium_valid_until']))
        self.db.execute('INSERT INTO storypushdevice VALUES (?,?,?,?,?,?,?,?,?,?)',
            tuple(self.device[k] for k in ('device_id','user_id','app_version','build_number','capability','enabled','language','time_zone','environment','updated_at')))
        self.db.commit()
        self.stories = [dict(id=f'{i:024x}', title=f'Title {i}', summary='A supported explanation.',
            transmission=f'MESSAGE {i}', receivedAt=utc_string(self.now-timedelta(minutes=10)),
            registration='N123', interestScore=90-i, notificationEligible=True, category='Operations', tier='major',
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
                          ('updated_at',0),('premium_valid_until',0)]:
            with self.subTest(key=key,value=value):
                self.assertFalse(eligible(dict(self.device,**{key:value}),self.now,'3.9.3',142))
        self.assertFalse(eligible(self.device,self.now,'',0))

    def test_daytime_slots_and_timezone(self):
        self.assertEqual(slot_for(self.now,'UTC'),('2026-09-28',0))
        self.assertIsNone(slot_for(self.now.replace(hour=23),'UTC')[1])
        self.assertEqual(slot_for(self.now.replace(hour=6),'Asia/Yerevan')[1],0)

    def test_three_per_day_spacing_and_no_repeat_across_devices(self):
        first=reserve(self.db,self.device,self.stories[0],self.now)
        self.assertIsNotNone(first)
        self.assertIsNone(reserve(self.db,self.device,self.stories[0],self.now))
        second=reserve(self.db,self.device,self.stories[1],self.now.replace(hour=15))
        third=reserve(self.db,self.device,self.stories[2],self.now.replace(hour=19))
        self.assertEqual(len({first,second,third}),3)
        self.assertIsNone(reserve(self.db,self.device,self.stories[3],self.now.replace(hour=20)))
        self.assertIsNone(reserve(self.db,self.device,self.stories[3],self.now+timedelta(days=1,minutes=-1)))
        fourth=reserve(self.db,self.device,self.stories[3],self.now+timedelta(days=1,minutes=1))
        self.assertIsNotNone(fourth)

    def test_independent_worker_cannot_duplicate_reservation(self):
        other=sqlite3.connect(self.database); other.row_factory=sqlite3.Row
        try:
            self.assertIsNotNone(reserve(self.db,self.device,self.stories[0],self.now))
            self.assertIsNone(reserve(other,self.device,self.stories[0],self.now))
        finally: other.close()

    def test_payload_uses_translation_and_no_fake_flight(self):
        value=payload(self.stories[0],'fr','notification')
        self.assertEqual(value['aps']['alert']['title'],'Un message')
        self.assertNotIn('flight_id',value)
        self.assertIsNone(payload(self.stories[0],'ar','notification'))

    def test_shared_campaign_is_frozen_and_delivered_to_each_device(self):
        day,slot=slot_for(self.now,'UTC')
        first=campaign_for(self.db,self.stories,day,slot,self.now)
        second=campaign_for(self.db,list(reversed(self.stories)),day,slot,self.now)
        self.assertEqual(first['id'],second['id'])
        self.assertIsNotNone(reserve(self.db,self.device,first,self.now))
        other=dict(self.device,device_id='tablet')
        self.assertIsNotNone(reserve(self.db,other,first,self.now))
        self.assertIsNone(reserve(self.db,other,first,self.now))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM storypushdelivery').fetchone()[0],2)

    def test_flight_alert_is_additive_and_not_repeated_after_shared_send(self):
        self.assertIsNotNone(reserve(self.db,self.device,self.stories[0],self.now,flight=True))
        self.assertIsNotNone(reserve(self.db,self.device,self.stories[1],self.now))
        self.assertIsNone(reserve(self.db,self.device,self.stories[1],self.now,flight=True))
        self.assertEqual(payload(dict(self.stories[0],flight='EK123'),'en','id',flight=True)['aps']['alert']['subtitle'],'EK123')

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

    def test_dispatch_shared_story_to_two_subscribers_and_extra_flight_story(self):
        self.db.execute('INSERT INTO user VALUES (?,?)',('second',self.device['premium_valid_until']))
        self.db.execute('INSERT INTO device VALUES (?,?,?,?)',('tablet','second','tablet-token',1))
        self.db.execute('INSERT INTO storypushdevice VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('tablet','second','3.9.4',143,1,1,'en','UTC','production',int(self.now.timestamp())))
        self.db.commit()
        with sqlite3.connect(self.feed) as db:
            story=dict(self.stories[1],flight='EK123')
            db.execute('UPDATE cockpit_stories SET payload=? WHERE id=?',(json.dumps(story),story['id']))
        calls=[]
        async def send(token,value,identifier):calls.append((token,value));return '200'
        with patch('core.services.story_push.load_targets',return_value=[{'id':1}]), \
             patch('core.services.story_push.matches',side_effect=lambda story,target:story['id']==self.stories[1]['id']):
            result=asyncio.run(dispatch(self.database,self.feed,send,enabled=True,
                releases={('3.9.3',142),('3.9.4',143)},now=self.now))
        self.assertEqual(result['accepted'],4)
        self.assertEqual(result['flight_reserved'],2)
        shared={v['story_id'] for _,v in calls if v['aps']['thread-id']=='aircraft-stories'}
        self.assertEqual(shared,{self.stories[0]['id']})
        self.assertEqual({token for token,_ in calls},{'test-token','tablet-token'})

    def test_expired_subscription_and_unmatched_flight_never_send(self):
        async def send(*args):self.fail('must not send')
        self.db.execute('UPDATE user SET premium_valid_until=?',(int((self.now-timedelta(seconds=1)).timestamp()*1000),))
        self.db.commit()
        result=asyncio.run(dispatch(self.database,self.feed,send,enabled=True,
                                    releases={('3.9.3',142)},now=self.now))
        self.assertEqual(result['eligible'],0)
        self.db.execute('UPDATE user SET premium_valid_until=?',(self.device['premium_valid_until'],))
        self.db.commit()
        with sqlite3.connect(self.feed) as db:
            story=dict(self.stories[1],flight='EK123')
            db.execute('UPDATE cockpit_stories SET payload=? WHERE id=?',(json.dumps(story),story['id']))
        calls=[]
        async def capture(token,value,identifier):calls.append(value);return '200'
        with patch('core.services.story_push.load_targets',return_value=[]):
            result=asyncio.run(dispatch(self.database,self.feed,capture,enabled=True,
                                        releases={('3.9.3',142)},now=self.now))
        self.assertEqual(result['flight_reserved'],0)
        self.assertEqual(len(calls),1)

    def test_testflight_qa_only_targets_one_opted_in_device_without_paid_entitlement(self):
        self.db.execute('UPDATE user SET premium_valid_until=NULL WHERE id=?',('owner',))
        self.db.execute("UPDATE storypushdevice SET environment='testflight' WHERE device_id='phone'")
        self.db.execute('INSERT INTO user VALUES (?,NULL)',('other',))
        self.db.execute('INSERT INTO device VALUES (?,?,?,?)',('tablet','other','tablet-token',1))
        self.db.execute('INSERT INTO storypushdevice VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('tablet','other','3.9.3',142,1,1,'en','UTC','testflight',int(self.now.timestamp())))
        self.db.commit()
        calls=[]
        async def send(token,value,identifier):calls.append(token);return '200'
        result=asyncio.run(dispatch(self.database,self.feed,send,enabled=True,
            environment='testflight',qa_device_id='phone',releases={('3.9.3',142)},now=self.now))
        self.assertEqual(result['accepted'],1)
        self.assertEqual(calls,['test-token'])
        self.assertFalse(eligible(dict(self.device,environment='testflight',premium_valid_until=None,
                                       enabled=0),self.now,'3.9.3',142,'testflight',qa_device_id='phone'))
        self.assertFalse(eligible(dict(self.device,environment='testflight',premium_valid_until=None,
                                       app_version='3.9.2'),self.now,'3.9.3',142,'testflight',qa_device_id='phone'))
        self.assertFalse(eligible(dict(self.device,environment='production',premium_valid_until=None),
                                  self.now,'3.9.3',142,qa_device_id='phone'))

    def test_testflight_scope_cannot_broadcast_or_bypass_production(self):
        async def send(*args):self.fail('must not send')
        with self.assertRaises(ValueError):
            asyncio.run(dispatch(self.database,self.feed,send,enabled=True,
                                 environment='testflight',releases={('3.9.3',142)},now=self.now))
        with self.assertRaises(ValueError):
            asyncio.run(dispatch(self.database,self.feed,send,enabled=True,
                                 environment='production',qa_device_id='phone',
                                 releases={('3.9.3',142)},now=self.now))

    def test_campaign_ranks_importance_not_category(self):
        def story(key,title,text,interest,category):
            return with_tier(dict(id=key*24,title=title,summary='An explanation.',transmission=text,
                                  receivedAt=utc_string(self.now-timedelta(minutes=10)),registration='N123',
                                  interestScore=interest,category=category,translations={}))
        holding=story('1','Holding over Frankfurt','HOLDING DUE TRAFFIC',95,'Diversion')
        strike=story('2','Bird strike on climb out','BIRD STRIKE ON CLIMB OUT',55,'Operations')
        routine=story('3','Ground power requested','NEED GPU UPON ARRIVAL',100,'Cabin')
        day,slot=slot_for(self.now,'UTC')
        # A major event wins over a higher-scored notable one and a top-scored routine one.
        self.assertEqual(campaign_for(self.db,[holding,strike,routine],day,slot,self.now)['id'],strike['id'])
        # Only background or below-floor stories: the slot is skipped, not filled.
        quiet=[routine,story('4','Holding briefly','HOLDING DUE TRAFFIC',65,'Operations')]
        later=self.now.replace(hour=15)
        self.assertIsNone(campaign_for(self.db,quiet,*slot_for(later,'UTC'),later))
        # Equal importance: a category not sent today breaks the tie.
        evening=self.now.replace(hour=19)
        same=story('5','Bird strike on approach','BIRD STRIKE ON APPROACH',55,'Operations')
        other=story('6','Lightning strike on descent','LIGHTNING STRIKE ON DESCENT',55,'Weather')
        self.assertEqual(campaign_for(self.db,[same,other],*slot_for(evening,'UTC'),evening)['id'],other['id'])

    def test_stale_and_unqualified_stories_do_not_fill_quota(self):
        async def send(*args): self.fail('must not send')
        with sqlite3.connect(self.feed) as db:
            db.execute("UPDATE cockpit_metadata SET value=?",(utc_string(self.now-timedelta(hours=3)),))
        result=asyncio.run(dispatch(self.database,self.feed,send,enabled=True,version='3.9.3',build=142,now=self.now))
        self.assertEqual(result['reserved'],0)

    def test_editorial_floor_accepts_legacy_false_flag_but_rejects_bulletin(self):
        good=dict(self.stories[0],interestScore=65,notificationEligible=False)
        bulletin=dict(self.stories[1],interestScore=90,transmission='ATIS BIRD STRIKE WARNING')
        with sqlite3.connect(self.feed) as db:
            for story in (good,bulletin):
                db.execute('UPDATE cockpit_stories SET payload=? WHERE id=?',(json.dumps(story),story['id']))
            for story in self.stories[2:]:
                db.execute('DELETE FROM cockpit_stories WHERE id=?',(story['id'],))
        self.assertEqual([s['id'] for s in candidates(self.feed,self.now)],[good['id']])


if __name__ == '__main__': unittest.main()
