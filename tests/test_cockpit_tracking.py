import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import httpx
from core.services.cockpit_tracking import load_targets, matches, number
from core.services import cockpit_ingestion as ingestion
from core.services.cockpit_stories import utc_string, read_stories


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.tmp.name)/'flight.db')
        self.cache=str(Path(self.tmp.name)/'cache.db')
        self.now=datetime.now(timezone.utc).replace(microsecond=0)
        db=sqlite3.connect(self.path)
        db.executescript('''CREATE TABLE flight(id,number,aircraft_reg,aircraft_modeS,airline_id,date);
          CREATE TABLE airline(id,iata,icao); CREATE TABLE userflightlink(user_id,flight_id);
          CREATE TABLE departure(flight_id,runway_time_utc,revised_time_utc,scheduled_time_utc);
          CREATE TABLE arrival(flight_id,runway_time_utc,revised_time_utc,scheduled_time_utc);
          INSERT INTO airline VALUES(1,'AA','AAL');
          INSERT INTO userflightlink VALUES('owner',1),('other',1);''')
        db.execute('INSERT INTO flight VALUES(1,?,?,?,?,?)',('AA123','N123AB','ABC123',1,self.now.date().isoformat()))
        db.execute('INSERT INTO departure VALUES(1,NULL,NULL,?)',(utc_string(self.now-timedelta(hours=1)),))
        db.execute('INSERT INTO arrival VALUES(1,NULL,NULL,?)',(utc_string(self.now+timedelta(hours=1)),))
        db.commit();db.close()
        self.targets=load_targets(self.path,now=self.now)

    def tearDown(self):self.tmp.cleanup()

    def message(self, **changes):
        return dict(dict(id='message',flight='AAL0123',registration='N123AB',
                         receivedAt=utc_string(self.now),text='NEED GPU UPON ARRIVAL APU INOP'),**changes)

    def test_shared_users_do_not_duplicate_targets_and_ownership(self):
        self.assertEqual(len(self.targets),1)
        self.assertEqual(load_targets(self.path,user_id='stranger',flight_id=1),(False,[]))
        self.assertTrue(load_targets(self.path,user_id='owner',flight_id=1)[0])

    def test_number_alias_and_time_and_aircraft_required(self):
        self.assertTrue(matches(self.message(),self.targets[0]))
        self.assertEqual(number('AA 00123'),'AA123')
        for changes in ({'flight':'AA124'},{'flight':None},{'registration':'N999AB'},
                        {'receivedAt':utc_string(self.now-timedelta(hours=3))}):
            self.assertFalse(matches(self.message(**changes),self.targets[0]))

    def test_missing_aircraft_and_expired_targets(self):
        self.assertEqual(load_targets(self.path,now=self.now+timedelta(days=2)),[])
        db=sqlite3.connect(self.path);db.execute('UPDATE flight SET aircraft_reg=NULL');db.commit();db.close()
        self.assertEqual(load_targets(self.path,user_id='owner',flight_id=1),(True,[]))

    def test_tracked_priority_before_global_without_duplicate_ai(self):
        db=ingestion.init_store(self.cache)
        for key,msg in [('global',self.message(flight='AA456')),('tracked',self.message())]:
            db.execute('INSERT INTO cockpit_queue VALUES(?,?,?,?)',(key,utc_string(self.now),json.dumps(msg),'pending'))
        db.commit()
        self.assertEqual(ingestion.pending_messages(db,self.targets)[0][0],'tracked')
        db.close()

    def test_read_filters_flight_before_limit(self):
        db=ingestion.init_store(self.cache)
        db.execute("INSERT INTO cockpit_metadata VALUES('updated_at',?)",(utc_string(self.now),))
        for i in range(60):
            msg=self.message(flight='AA999' if i else 'AA123')
            db.execute('INSERT INTO cockpit_stories VALUES(?,?,?,?)',(str(i),msg['receivedAt'],msg['registration'],json.dumps(msg)))
        db.commit();db.close()
        self.assertEqual(len(read_stories(self.cache,flight=self.targets[0])['stories']),1)

    def test_provider_filter_dedupe_and_wrong_flight_rejected(self):
        requests=[]
        def handle(req):
            requests.append(req)
            return httpx.Response(200,json=[dict(id=i,createdAt=utc_string(self.now),timestamp=utc_string(self.now),tail='N123AB',flightNumber=flight,text='NEED GPU UPON ARRIVAL APU INOP') for i,flight in [(1,'AA123'),(2,'AA999')]])
        async def execute():
            db=ingestion.init_store(self.cache)
            metrics={'provider_requests':0}
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                await ingestion.ingest_tracked(db,self.targets*2,'secret',client,metrics,ingestion.time.monotonic(),self.now)
            count=db.execute('SELECT COUNT(*) FROM cockpit_queue').fetchone()[0];db.close()
            return count,metrics
        count,metrics=asyncio.run(execute())
        self.assertEqual(count,1)
        self.assertEqual(metrics['tracked_requests'],1)
        self.assertEqual(requests[0].url.params['icao'],'ABC123')

    def test_tracked_rate_limit_does_not_advance_cursor(self):
        async def execute():
            db=ingestion.init_store(self.cache)
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(429))) as client:
                with self.assertRaises(ValueError):
                    await ingestion.ingest_tracked(db,self.targets,'key',client,{'provider_requests':0},ingestion.time.monotonic(),self.now)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cockpit_cursor').fetchone()[0],0)
            db.close()
        asyncio.run(execute())

    def test_tracked_requests_bounded_and_next_run_rotates_aircraft(self):
        targets=[dict(self.targets[0],registration=f'N{i:04}AB') for i in range(10)]
        async def execute():
            db=ingestion.init_store(self.cache)
            metrics={'provider_requests':0}
            with patch('core.services.cockpit_ingestion.asyncio.sleep',return_value=None):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=[]))) as client:
                    await ingestion.ingest_tracked(db,targets,'key',client,metrics,ingestion.time.monotonic(),self.now)
                    self.assertEqual(metrics['tracked_requests'],8)
                    await ingestion.ingest_tracked(db,targets,'key',client,metrics,ingestion.time.monotonic(),self.now+timedelta(minutes=10))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cockpit_metadata WHERE key LIKE 'tracked:%'").fetchone()[0],10)
            db.close()
        asyncio.run(execute())

if __name__=='__main__':unittest.main()
