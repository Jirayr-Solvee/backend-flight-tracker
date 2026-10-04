import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from core.services.cockpit_importance import (alert_worthy, assign_importance, classify, diversify, importance_key,
                                              one_per_aircraft, rank_stories, with_tier)
from core.services.cockpit_stories import open_store


def story(key, title, text, interest, received='2026-10-04T10:00:00Z'):
    return dict(id=key, title=title, transmission=text, interestScore=interest, receivedAt=received, registration='N' + key)


class ImportanceTests(unittest.TestCase):
    def test_kinds_read_like_a_reader(self):
        cases = {
            'safety': [('Smoke reported in the cabin', 'SMOKE IN AFT GALLEY'),
                       ('Engine failure, diverting to Denver', 'ENG 2 FAILURE DIVERTING TO KDEN'),
                       ('Bird strike damages windshield', 'BIRD STRIKE CRACKED WINDSHIELD')],
            'strike': [('Bird Strike Reported on Right Wing During Climb Out', 'BIRD STRIKE ON CLIMB OUT'),
                       ('Lightning Strike Reported During Climb', 'LIGHTNING STRIKE ON CLB'),
                       ('Laser illumination reported', 'LASER ILLUMINATION ON FINAL'),
                       ('Bird strike, no damage', 'BIRD STRIKE NO DAMAGE FOUND ENG CHECKED')],
            'crew': [('A birthday wish from the cockpit', 'ITS OUR FO BIRTHDAY'),
                     ('Captain retires after 35 years', 'CAPT RETIREMENT FLIGHT')],
            'cabin': [('A smell in the galley', 'SMELL SEEMS TO BE GONE'),
                      ('Volcanic Ash Smell Reported', 'VA SMELL'),
                      ('Coffee maker inoperative', 'COFFEE MAKER INOP')],
            'cargo': [('Horses on board', 'SPECIAL HANDLING HORSES ON BOARD')],
            # Diversions and weather deviations are the same thing to a reader.
            'route': [('Aircraft Diverting to HER Due to Thunderstorms', 'DUE TO TS OVER CHQ WE ARE DIVERTING TO HER'),
                      ('Aircraft Deviates for Weather North of Course', 'DEVIATING NORTH AROUND WX'),
                      ('Holding over Frankfurt', 'HOLDING DUE TRAFFIC'),
                      ('Diversion planned', 'WE PLAN ON DIVERTING TO RSW DUE ENG INDICATION')],
            'weather': [('Moderate Turbulence Reported at FL340', 'MOD TURB FL340'),
                        ('Convective Weather Line Approaching', 'CONVECTIVE LINE')],
            'ground': [('APU Inoperative, Requesting Ground Power', 'APU INOP NEED GPU'),
                       ('UA4199/04 Experiences Lengthy Tarmac Delay at KORD',
                        'LENGTHY TARMAC DELAY OFF GATE FOR 60 MINS. FLIGHT MUST BE AIRBORNE OR RETURNING TO GATE OR EGRESS')],
        }
        for kind, examples in cases.items():
            for title, text in examples:
                self.assertEqual(classify(story('x', title, text, 70))[0], kind, title)

    def test_negated_and_conditional_actions_do_not_count(self):
        for text in ('NOT DIVERTING, CONTINUING TO DEST', 'IF DIVERTING ADVISE OPS',
                     'NO LONGER DIVERTING', 'UNABLE TO RETURN TO KIAH'):
            self.assertEqual(classify(story('n', 'Crew update', text, 70))[0], 'other', text)

    def test_score_adjusts_interest_by_kind_and_wording(self):
        self.assertEqual(classify(story('a', 'Bird strike on climb', 'BIRD STRIKE ON CLIMB', 70))[1], 80)
        self.assertEqual(classify(story('b', 'APU inoperative', 'APU INOP', 70))[1], 50)
        self.assertEqual(classify(story('c', 'Turbulence forecast', 'TURBULENCE FORECAST FOR ROUTE', 70))[1], 55)
        self.assertEqual(classify(story('d', 'Possible diversion', 'MAY DIVERT TO MKE', 70))[1], 55)
        self.assertEqual(classify(story('e', 'Lengthy tarmac delay', 'LENGTHY TARMAC DELAY OFF GATE FOR 150 MINS', 70))[1], 65)
        self.assertEqual(classify(story('g', 'Diverting to HER', 'DIVERTING TO HER', 70))[1], 65)
        self.assertEqual(classify(story('f', 'Small bird strike', 'SMALL BIRD STRIKE ALL OK', 40))[1], 50)

    def test_serious_events_are_major_on_their_own(self):
        for title, text in [('Smoke reported in the cabin', 'SMOKE IN AFT GALLEY'),
                            ('Engine failure, diverting', 'ENG 2 FAILURE DIVERTING TO KDEN'),
                            ('Emergency descent', 'EMERGENCY DESCENT CABIN DEPRESSURIZED'),
                            ('Rejected takeoff at JFK', 'REJECTED TAKEOFF RWY 4L')]:
            self.assertEqual(with_tier(story('x', title, text, 60))['tier'], 'major', title)
        # A minor note in serious wording is notable, not major.
        self.assertEqual(with_tier(story('y', 'Smoke reported', 'SMOKE IN CABIN', 40))['tier'], 'notable')

    def test_each_day_gets_one_or_two_highlights_of_different_kinds(self):
        busy = [story('1', 'Diverting to HER due to storms', 'DIVERTING TO HER DUE TS', 75, '2026-10-04T09:00:00Z'),
                story('2', 'Diverting to MDZ', 'DIVERTING TO MDZ', 74, '2026-10-04T10:00:00Z'),
                story('3', 'Bird strike on climb out', 'BIRD STRIKE ON CLIMB OUT', 62, '2026-10-04T11:00:00Z'),
                story('4', 'APU inoperative', 'APU INOP NEED GPU', 60, '2026-10-04T12:00:00Z'),
                story('5', 'Moderate turbulence', 'MOD TURB FL340', 60, '2026-10-04T13:00:00Z')]
        quiet = [story('6', 'APU inoperative', 'APU INOP', 60, '2026-10-03T10:00:00Z'),
                 story('7', 'Turbulence forecast', 'TURBULENCE FORECAST', 60, '2026-10-03T11:00:00Z'),
                 # Notable, but ground operations are never a highlight.
                 story('12', 'Lengthy tarmac delay', 'LENGTHY TARMAC DELAY OFF GATE FOR 150 MINS', 75, '2026-10-03T12:00:00Z')]
        serious = [story('8', 'Smoke in the cabin', 'SMOKE IN CABIN', 70, '2026-10-02T09:00:00Z'),
                   story('9', 'Diverting to KIAH', 'DIVERTING TO KIAH', 70, '2026-10-02T10:00:00Z'),
                   story('10', 'Holding over KIAH', 'HOLDING DUE TFC', 65, '2026-10-02T11:00:00Z'),
                   story('11', 'A birthday in the cockpit', 'FO BIRTHDAY', 55, '2026-10-02T12:00:00Z')]
        ranked = rank_stories(busy + quiet + serious)
        self.assertEqual({key for key, rank in ranked.items() if rank['tier'] == 'major'}, {'1', '3', '8', '9', '11'})
        self.assertEqual((ranked['2']['tier'], ranked['5']['tier'], ranked['10']['tier']), ('notable', 'notable', 'notable'))
        self.assertEqual((ranked['4']['tier'], ranked['6']['tier'], ranked['7']['tier']), ('background',) * 3)
        self.assertEqual(ranked['12']['tier'], 'notable')

    def test_refresh_stores_ranking_and_reads_use_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = open_store(str(Path(tmp) / 'feed.sqlite'))
            try:
                for item in (story('1', 'Diverting to HER', 'DIVERTING TO HER', 75),
                             story('2', 'APU inoperative', 'APU INOP', 60)):
                    db.execute('INSERT INTO cockpit_stories VALUES (?,?,?,?)',
                               (item['id'], item['receivedAt'], item['registration'], json.dumps(item)))
                db.commit()
                self.assertEqual(assign_importance(db), 2)
                self.assertEqual(assign_importance(db), 0)
                stored = json.loads(db.execute("SELECT payload FROM cockpit_stories WHERE id='1'").fetchone()[0])
                self.assertEqual((stored['tier'], stored['kind'], stored['importance']), ('major', 'route', 70))
                self.assertIs(with_tier(stored), stored)
            finally:
                db.close()
        # Not ranked yet: kind and score now, no daily highlight.
        self.assertEqual(with_tier(story('3', 'Diverting to HER', 'DIVERTING TO HER', 75))['tier'], 'notable')

    def test_tier_outranks_importance_and_fresher_wins_within_a_tier(self):
        now = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)
        older = dict(tier='notable', importance=75, receivedAt='2026-10-02T18:00:00Z', id='old')
        today = dict(tier='notable', importance=70, receivedAt='2026-10-04T09:00:00Z', id='new')
        major = dict(tier='major', importance=60, receivedAt='2026-10-02T17:00:00Z', id='major')
        background = dict(tier='background', importance=100, receivedAt='2026-10-04T17:00:00Z', id='bg')
        ranked = sorted([older, background, major, today], key=lambda s: importance_key(s, now), reverse=True)
        self.assertEqual([s['id'] for s in ranked], ['major', 'new', 'old', 'bg'])

    def test_top_keeps_one_story_per_aircraft_and_never_repeats_a_kind(self):
        def s(key, registration, kind, tier='notable'):
            return dict(id=key, registration=registration, kind=kind, tier=tier)
        stories = [s('1', 'N4199', 'route'), s('2', 'N-4199', 'route'), s('3', 'N1', 'route'), s('4', 'N2', 'weather'),
                   s('5', 'N3', 'route'), s('6', 'N4', 'strike'), s('7', 'N5', 'route', tier='background')]
        unique = one_per_aircraft(stories)
        self.assertEqual([story['id'] for story in unique], ['1', '3', '4', '5', '6', '7'])
        self.assertEqual([story['id'] for story in diversify(unique)], ['1', '4', '3', '6', '5', '7'])

    def test_alert_floor(self):
        self.assertTrue(alert_worthy(dict(tier='major', importance=40)))
        self.assertTrue(alert_worthy(dict(tier='notable', importance=70)))
        self.assertFalse(alert_worthy(dict(tier='notable', importance=69)))
        self.assertFalse(alert_worthy(dict(tier='background', importance=100)))


if __name__ == '__main__':
    unittest.main()
