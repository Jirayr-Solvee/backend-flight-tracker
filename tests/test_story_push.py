import asyncio
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from core.services.story_push import eligible, slot_for, reserve, dispatch, payload, campaign_for, candidates
from core.services.cockpit_stories import open_store, utc_string
from core.services.cockpit_importance import with_tier
from tests.story_push_fixtures import create_subscription_tables, add_subscription, add_lifecycle


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
        create_subscription_tables(self.db)
        add_subscription(self.db,int(self.now.timestamp()*1000))
        self.db.execute('INSERT INTO device VALUES (?,?,?,?)', ('phone','owner','test-token',1))
        self.db.execute('INSERT INTO user VALUES (?,?)',('owner',self.device['premium_valid_until']))
        self.db.execute('INSERT INTO storypushdevice VALUES (?,?,?,?,?,?,?,?,?,?)',
            tuple(self.device[k] for k in ('device_id','user_id','app_version','build_number','capability','enabled','language','time_zone','environment','updated_at')))
        self.db.commit()
        self.stories = [dict(id=f'{i:024x}', title=f'Title {i}', summary='A supported explanation.',
            transmission=f'SMOKE IN CABIN {i}', receivedAt=utc_string(self.now-timedelta(minutes=10)),
            registration='N123', interestScore=90-i, notificationEligible=True, category='Operations',
            translations={'fr':dict(title='Un message',summary='Une explication.')} ) for i in range(6)]
        with open_store(self.feed) as db:
            db.execute("INSERT INTO cockpit_metadata VALUES ('updated_at',?)",(utc_string(self.now),))
            for s in self.stories:
                db.execute('INSERT INTO cockpit_stories VALUES (?,?,?,?)',(s['id'],s['receivedAt'],s['registration'],json.dumps(s)))

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def test_exact_version_and_capability_fail_closed(self):
        self.assertTrue(eligible(self.device,self.now,'3.9.3',142,subscription_access=True))
        for key,value in [('build_number',141),('build_number',143),('app_version','3.9.2'),
                          ('capability',0),('enabled',0),('apn_token_active',0),('apn_token',''),
                          ('owner_id','other'),('environment','testflight'),('environment','development'),
                          ('updated_at',0)]:
            with self.subTest(key=key,value=value):
                self.assertFalse(eligible(dict(self.device,**{key:value}),self.now,'3.9.3',142,subscription_access=True))
        self.assertFalse(eligible(self.device,self.now,'',0))
        self.assertFalse(eligible(self.device,self.now,'3.9.3',142))
        self.assertFalse(eligible(self.device,self.now,'3.9.3',142,subscription_access='true'))

    def test_daytime_slots_and_timezone(self):
        self.assertEqual(slot_for(self.now,'UTC'),('2026-09-28',0))
        self.assertIsNone(slot_for(self.now.replace(hour=23),'UTC')[1])
        self.assertEqual(slot_for(self.now.replace(hour=6),'Asia/Yerevan')[1],0)

    def test_one_per_day_across_slots_and_devices(self):
        first=reserve(self.db,self.device,self.stories[0],self.now)
        self.assertIsNotNone(first)
        self.assertIsNone(reserve(self.db,self.device,self.stories[0],self.now))
        self.assertIsNone(reserve(self.db,self.device,self.stories[1],self.now.replace(hour=15)))
        self.assertIsNone(reserve(self.db,dict(self.device,device_id='tablet'),self.stories[2],
                                  self.now.replace(hour=19),flight=True))
        self.assertIsNone(reserve(self.db,self.device,self.stories[3],self.now+timedelta(days=1,minutes=-1)))
        fourth=reserve(self.db,self.device,self.stories[3],self.now+timedelta(days=1,minutes=1))
        self.assertIsNotNone(fourth)

    def test_fall_back_day_does_not_allow_second_push(self):
        first=datetime(2026,11,1,4,15,tzinfo=timezone.utc)
        device=dict(self.device,time_zone='America/New_York')
        self.assertIsNotNone(reserve(self.db,device,self.stories[0],first,flight=True))
        self.assertIsNone(reserve(self.db,device,self.stories[1],first+timedelta(hours=24,minutes=15),flight=True))
        self.assertIsNotNone(reserve(self.db,device,self.stories[1],first+timedelta(hours=25),flight=True))

    def test_independent_worker_cannot_duplicate_reservation(self):
        other=sqlite3.connect(self.database); other.row_factory=sqlite3.Row
        try:
            self.assertIsNotNone(reserve(self.db,self.device,self.stories[0],self.now))
            self.assertIsNone(reserve(other,dict(self.device,device_id='tablet'),self.stories[1],self.now,flight=True))
        finally: other.close()

    def test_concurrent_workers_share_daily_quota(self):
        ready = Barrier(2)
        def attempt(index):
            with sqlite3.connect(self.database, timeout=5) as connection:
                connection.row_factory = sqlite3.Row
                ready.wait(timeout=5)
                return reserve(connection, dict(self.device, device_id=f'device{index}'),
                               self.stories[index], self.now, flight=bool(index))
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(result is not None for result in pool.map(attempt, range(2))), 1)

    def test_payload_uses_translation_and_no_fake_flight(self):
        value=payload(self.stories[0],'fr','notification')
        self.assertEqual(value['aps']['alert']['title'],'Un message')
        self.assertNotIn('flight_id',value)
        self.assertIsNone(payload(self.stories[0],'ar','notification'))

    def test_shared_campaign_is_frozen_and_delivered_once_per_account(self):
        day,slot=slot_for(self.now,'UTC')
        first=campaign_for(self.db,self.stories,day,slot,self.now)
        second=campaign_for(self.db,list(reversed(self.stories)),day,slot,self.now)
        self.assertEqual(first['id'],second['id'])
        self.assertIsNotNone(reserve(self.db,self.device,first,self.now))
        self.assertIsNone(reserve(self.db,dict(self.device,device_id='tablet'),first,self.now))
        other=dict(self.device,device_id='tablet',user_id='other')
        self.assertIsNotNone(reserve(self.db,other,first,self.now))
        self.assertIsNone(reserve(self.db,other,first,self.now))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM storypushdelivery').fetchone()[0],2)

    def test_shared_and_flight_alerts_consume_same_daily_cap(self):
        self.assertIsNotNone(reserve(self.db,self.device,self.stories[0],self.now,flight=True))
        self.assertIsNone(reserve(self.db,self.device,self.stories[1],self.now))
        self.assertIsNone(reserve(self.db,self.device,self.stories[1],self.now,flight=True))
        other=dict(self.device,device_id='tablet',user_id='other')
        self.assertIsNotNone(reserve(self.db,other,self.stories[0],self.now))
        self.assertIsNone(reserve(self.db,other,self.stories[1],self.now,flight=True))
        self.assertEqual(payload(dict(self.stories[0],flight='EK123'),'en','id',flight=True)['aps']['alert']['subtitle'],'EK123')

    def test_disabled_never_opens_database_or_calls_sender(self):
        async def send(*args): self.fail('must not send')
        result=asyncio.run(dispatch('/missing','/missing',send))
        self.assertEqual(result['reserved'],0)

    def test_expired_api_lease_does_not_suppress_verified_subscriber(self):
        self.db.execute('UPDATE user SET premium_valid_until=0');self.db.commit()
        calls=[]
        async def send(*args):calls.append(args);return '200'
        result=asyncio.run(dispatch(self.database,self.feed,send,enabled=True,
            version='3.9.3',build=142,now=self.now))
        self.assertEqual(result['accepted'],1)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.db.execute('SELECT premium_valid_until FROM user').fetchone()[0],0)

    def test_fresh_api_lease_never_substitutes_for_verified_subscription(self):
        self.db.execute('DELETE FROM "transaction"');self.db.commit()
        async def send(*args):self.fail('cache alone cannot grant')
        result=asyncio.run(dispatch(self.database,self.feed,send,enabled=True,
            version='3.9.3',build=142,now=self.now))
        self.assertEqual(result['eligible'],0)

    def test_concurrent_changes_after_reservation_suppress_before_apns(self):
        # A separate connection commits between reservation and the second
        # entitlement/device check, as a webhook or preference update would.
        for change in ('refund','unlink','disable','token','owner','expiration'):
            with self.subTest(change=change):
                def changed_reserve(db,device,story,now,**kwargs):
                    delivery=reserve(db,device,story,now,**kwargs)
                    with sqlite3.connect(self.database) as other:
                        if change=='refund':other.execute('UPDATE "transaction" SET revoked_date=1')
                        elif change=='unlink':other.execute('DELETE FROM usersubscriptionlink')
                        elif change=='disable':other.execute('UPDATE storypushdevice SET enabled=0')
                        elif change=='token':other.execute("UPDATE device SET apn_token='new-token'")
                        elif change=='owner':other.execute("UPDATE device SET user_id='different-owner'")
                        else:add_lifecycle(other,int(self.now.timestamp()*1000),event_kind='expiration',renewal_signed_date_ms=None)
                    return delivery
                async def send(*args):self.fail('must recheck committed change')
                with patch('core.services.story_push.reserve',side_effect=changed_reserve):
                    result=asyncio.run(dispatch(self.database,self.feed,send,enabled=True,
                        version='3.9.3',build=142,now=self.now))
                self.assertEqual(result['accepted'],0)
                self.assertEqual(self.db.execute('SELECT status FROM storypushdelivery').fetchone()[0],'suppressed')
                self.db.execute('DELETE FROM storypushdelivery')
                self.db.execute('DELETE FROM appstoresubscriptionlifecycleevent')
                self.db.execute('UPDATE "transaction" SET revoked_date=NULL')
                self.db.execute("INSERT OR IGNORE INTO usersubscriptionlink VALUES ('owner','chain')")
                self.db.execute('UPDATE storypushdevice SET enabled=1')
                self.db.execute("UPDATE device SET apn_token='test-token',user_id='owner'")
                self.db.commit()

    def test_wall_clock_expiry_is_rechecked_after_reservation(self):
        self.db.execute('UPDATE "transaction" SET expires_date=?',(int(self.now.timestamp()*1000)+1000,));self.db.commit()
        async def send(*args):self.fail('subscription expired during run')
        with patch('core.services.story_push.time') as clock:
            clock.monotonic.side_effect=[100,100,100,102]
            result=asyncio.run(dispatch(self.database,self.feed,send,enabled=True,
                version='3.9.3',build=142,now=self.now))
        self.assertEqual(result['accepted'],0)
        self.assertEqual(self.db.execute('SELECT status FROM storypushdelivery').fetchone()[0],'suppressed')

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

    def test_dispatch_supported_310_prioritizes_flight_story_under_cap(self):
        self.db.execute('INSERT INTO user VALUES (?,?)',('second',self.device['premium_valid_until']))
        add_subscription(self.db,int(self.now.timestamp()*1000),user_id='second',chain='second',tx='second')
        self.db.execute('INSERT INTO device VALUES (?,?,?,?)',('tablet','second','tablet-token',1))
        self.db.execute('INSERT INTO storypushdevice VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('tablet','second','3.10',146,1,1,'en','UTC','production',int(self.now.timestamp())))
        self.db.commit()
        with sqlite3.connect(self.feed) as db:
            story=dict(self.stories[1],flight='EK123')
            db.execute('UPDATE cockpit_stories SET payload=? WHERE id=?',(json.dumps(story),story['id']))
        calls=[]
        async def send(token,value,identifier):calls.append((token,value));return '200'
        with patch('core.services.story_push.load_targets',return_value=[{'id':1}]), \
             patch('core.services.story_push.matches',side_effect=lambda story,target:story['id']==self.stories[1]['id']):
            result=asyncio.run(dispatch(self.database,self.feed,send,enabled=True,
                releases={('3.9.3',142),('3.10',146)},now=self.now))
        self.assertEqual(result['accepted'],2)
        self.assertEqual(result['flight_reserved'],2)
        self.assertTrue(all(v['aps']['thread-id']=='flight-messages' for _,v in calls))
        self.assertEqual({token for token,_ in calls},{'test-token','tablet-token'})

    def test_expired_subscription_and_unmatched_flight_never_send(self):
        async def send(*args):self.fail('must not send')
        self.db.execute('UPDATE "transaction" SET expires_date=?',(int((self.now-timedelta(seconds=1)).timestamp()*1000),))
        self.db.commit()
        result=asyncio.run(dispatch(self.database,self.feed,send,enabled=True,
                                    releases={('3.9.3',142)},now=self.now))
        self.assertEqual(result['eligible'],0)
        self.db.execute('UPDATE "transaction" SET expires_date=?',(self.device['premium_valid_until'],))
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
        def story(key,title,text,interest,category,at=None):
            return with_tier(dict(id=key*24,title=title,summary='An explanation.',transmission=text,
                                  receivedAt=utc_string((at or self.now)-timedelta(minutes=10)),registration='N123',
                                  interestScore=interest,category=category,translations={}))
        diversion=story('1','Diverting to HER due to storms','DIVERTING TO HER DUE TS',95,'Diversion')
        engine=story('2','Engine failure, diverting','ENG 2 FAILURE DIVERTING TO KDEN',60,'Operations')
        routine=story('3','Ground power requested','NEED GPU UPON ARRIVAL',60,'Cabin')
        day,slot=slot_for(self.now,'UTC')
        # A major event wins over a higher-scored notable one and a routine one.
        self.assertEqual(campaign_for(self.db,[diversion,engine,routine],day,slot,self.now)['id'],engine['id'])
        # Only background or below-floor stories: the slot is skipped, not filled.
        quiet=[routine,story('4','Holding briefly','HOLDING DUE TRAFFIC',69,'Weather')]
        later=self.now.replace(hour=15)
        self.assertIsNone(campaign_for(self.db,quiet,*slot_for(later,'UTC'),later))
        # Next day, after a route-change alert, another kind goes first despite a lower score.
        tomorrow=self.now+timedelta(days=1)
        first=story('5','Diverting to EDDV','DIVERTING TO EDDV',90,'Diversion',tomorrow)
        self.assertEqual(campaign_for(self.db,[first],*slot_for(tomorrow,'UTC'),tomorrow)['id'],first['id'])
        evening=tomorrow.replace(hour=19)
        route=story('6','Diverting to MDZ','DIVERTING TO MDZ',92,'Diversion',evening)
        cabin=story('7','Volcanic ash smell reported','VOLCANIC ASH SMELL',66,'Weather',evening)
        self.assertEqual(campaign_for(self.db,[first,route,cabin],*slot_for(evening,'UTC'),evening)['id'],cabin['id'])
        # An alert is news: a day-old story is never sent, however important.
        day_after=tomorrow+timedelta(days=1)
        old=story('8','Smoke in the cabin','SMOKE IN CABIN',90,'Cabin',day_after-timedelta(hours=25))
        self.assertIsNone(campaign_for(self.db,[old],*slot_for(day_after,'UTC'),day_after))

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
