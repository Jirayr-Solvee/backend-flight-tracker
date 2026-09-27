import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from core.services.cockpit_stories import open_store,read_stories,utc_string,decode_cursor

class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.temp.name)/'feed.db')
        self.now=datetime.now(timezone.utc).replace(microsecond=0)
        self.db=open_store(self.path)
        self.db.execute("INSERT INTO cockpit_metadata VALUES ('updated_at',?)",(utc_string(self.now),))
        for i in range(75):self.insert(i,self.now-timedelta(minutes=i//3))
        self.db.commit()
    def tearDown(self):self.db.close();self.temp.cleanup()
    def insert(self,i,when):
        story=dict(id=f'{i:04}',receivedAt=utc_string(when),registration='N123AB',flight='AA123',category='Weather' if i%2 else 'Cargo')
        self.db.execute('INSERT INTO cockpit_stories VALUES (?,?,?,?)',(story['id'],story['receivedAt'],story['registration'],json.dumps(story)))
    def test_all_pages_with_ties_and_new_arrival_no_duplicates(self):
        first=read_stories(self.path,limit=20)
        ids=[s['id'] for s in first['stories']]
        self.insert(100,self.now+timedelta(seconds=1));self.db.commit()
        cursor=first['nextCursor']
        while cursor:
            page=read_stories(self.path,limit=20,cursor=cursor)
            self.assertLessEqual(len(page['stories']),20)
            ids.extend(s['id'] for s in page['stories']);cursor=page['nextCursor']
        self.assertEqual(len(ids),75);self.assertEqual(len(set(ids)),75)
        self.assertNotIn('0100',ids)
    def test_category_applied_before_limit_and_across_pages(self):
        results=[];cursor=None
        while True:
            page=read_stories(self.path,category='Weather',limit=7,cursor=cursor)
            results.extend(page['stories']);cursor=page['nextCursor']
            if not cursor:break
        self.assertEqual(len(results),37)
        self.assertTrue(all(s['category']=='Weather' for s in results))
    def test_flight_scope_retention_and_empty(self):
        target=dict(registration='N123AB',aliases={'AA123'},start=self.now-timedelta(minutes=4),end=self.now)
        first=read_stories(self.path,flight=target,limit=3)
        second=read_stories(self.path,flight=target,limit=20,cursor=first['nextCursor'])
        self.assertEqual(len(first['stories'])+len(second['stories']),15)
        self.insert(200,self.now-timedelta(days=8));self.db.commit()
        self.assertEqual(read_stories(self.path,registration='OTHER')['stories'],[])
        self.assertIsNone(read_stories(self.path,registration='OTHER')['nextCursor'])
        self.assertNotIn('0200',[s['id'] for s in read_stories(self.path)['stories']])
    def test_bad_cursor(self):
        for cursor in ('garbage','e30','W10'):
            with self.assertRaises(ValueError):decode_cursor(cursor)

if __name__=='__main__':unittest.main()
