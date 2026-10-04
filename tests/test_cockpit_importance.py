import unittest
from core.services.cockpit_importance import alert_worthy, importance_key, story_tier, with_tier


class ImportanceTests(unittest.TestCase):
    def test_event_type_decides_tier(self):
        cases = {
            'major': [('Flight Diverts to Denver', 'DIVERTING TO KDEN DUE WX'),
                      ('Bird Strike Reported on Flight JL752', 'BIRD STRIKE ON TAKEOFF'),
                      ('Aircraft Struck by Lightning Departing Phoenix, No Issues Reported', ''),
                      ('Return to departure airport', 'RETURNING TO KIAH'),
                      ('Smoke reported in the cabin', 'SMOKE IN AFT GALLEY'),
                      ('Go-around at JFK', 'GO AROUND RWY 4R')],
            'notable': [('UA4199/04 Experiences Lengthy Tarmac Delay at KORD',
                         'LENGTHY TARMAC DELAY OFF GATE FOR 60 MINS. FLIGHT MUST BE AIRBORNE OR RETURNING TO GATE OR EGRESS'),
                        ('Holding over Frankfurt', 'HOLDING DUE TRAFFIC'),
                        ('A birthday wish from the cockpit', 'ITS OUR FO BIRTHDAY'),
                        ('Back to the gate', 'RETURNING TO GATE DUE MX'),
                        ('A smell in the galley', 'SMELL SEEMS TO BE GONE'),
                        ('Deviating around weather', 'DEVIATING NORTH AROUND WEATHER')],
            'background': [('APU Inoperative, No Thrust Reverser Credit', 'APU INOP'),
                           ('Turbulence Warning for Sydney Airport Runway 34R', 'POSSIBLE TURBULENCE ON FINAL'),
                           ('Aircraft back in service', 'ACFT RETURNED TO SERVICE AFTER MX'),
                           ('Ground power requested', 'NEED GPU UPON ARRIVAL'),
                           ('', '')],
        }
        for tier, examples in cases.items():
            for title, text in examples:
                self.assertEqual(story_tier(title, text), tier, title)

    def test_negated_and_conditional_actions_do_not_count(self):
        for text in ('NOT DIVERTING, CONTINUING TO DEST', 'IF DIVERTING ADVISE OPS',
                     'NO LONGER DIVERTING', 'UNABLE TO RETURN TO KIAH'):
            self.assertEqual(story_tier('Crew update', text), 'background', text)

    def test_stored_tier_is_kept_and_older_stories_are_derived(self):
        self.assertEqual(with_tier(dict(title='Holding', tier='major'))['tier'], 'major')
        self.assertEqual(with_tier(dict(title='Flight diverts', transmission='DIVERTING TO KDEN'))['tier'], 'major')
        self.assertEqual(with_tier(dict(title='Holding', tier='bogus'))['tier'], 'notable')

    def test_tier_outranks_interest(self):
        major = dict(tier='major', interestScore=40, receivedAt='2026-10-04T10:00:00Z', id='a')
        notable = dict(tier='notable', interestScore=95, receivedAt='2026-10-04T11:00:00Z', id='b')
        background = dict(tier='background', interestScore=100, receivedAt='2026-10-04T12:00:00Z', id='c')
        ranked = sorted([background, notable, major], key=importance_key, reverse=True)
        self.assertEqual([story['id'] for story in ranked], ['a', 'b', 'c'])

    def test_fresher_story_wins_within_a_tier_but_never_across_tiers(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)
        older = dict(tier='major', interestScore=75, receivedAt='2026-10-02T18:00:00Z', id='old')
        today = dict(tier='major', interestScore=70, receivedAt='2026-10-04T09:00:00Z', id='new')
        fresh_notable = dict(tier='notable', interestScore=95, receivedAt='2026-10-04T17:59:00Z', id='notable')
        ranked = sorted([older, fresh_notable, today], key=lambda s: importance_key(s, now), reverse=True)
        self.assertEqual([story['id'] for story in ranked], ['new', 'old', 'notable'])

    def test_alert_floor_by_tier(self):
        self.assertTrue(alert_worthy(dict(tier='major', interestScore=50)))
        self.assertFalse(alert_worthy(dict(tier='major', interestScore=49)))
        self.assertTrue(alert_worthy(dict(tier='notable', interestScore=70)))
        self.assertFalse(alert_worthy(dict(tier='notable', interestScore=69)))
        self.assertFalse(alert_worthy(dict(tier='background', interestScore=100)))
        self.assertFalse(alert_worthy(dict(tier='major', interestScore='90')))


if __name__ == '__main__':
    unittest.main()
