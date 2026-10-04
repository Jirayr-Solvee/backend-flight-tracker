import unittest
from core.services.cockpit_importance import (alert_worthy, diversify, importance_key, one_per_aircraft,
                                              story_tier, with_tier)


class ImportanceTests(unittest.TestCase):
    def test_major_is_reserved_for_serious_events(self):
        for title, text in [('Smoke reported in the cabin', 'SMOKE IN AFT GALLEY'),
                            ('Engine failure, diverting to Denver', 'ENG 2 FAILURE DIVERTING TO KDEN'),
                            ('Return to departure airport', 'HYD FAILURE RETURNING TO KIAH'),
                            ('Bird strike damages windshield', 'BIRD STRIKE CRACKED WINDSHIELD'),
                            ('Rejected takeoff at JFK', 'REJECTED TAKEOFF RWY 4L'),
                            ('Emergency descent', 'EMERGENCY DESCENT CABIN DEPRESSURIZED')]:
            self.assertEqual(story_tier(title, text, 70), 'major', title)

    def test_everyday_events_are_notable(self):
        # Observed in production: weather, fuel and closed-airport diversions,
        # uneventful strikes and possible or planned diversions.
        for title, text in [('Aircraft Diverting to HER Due to Thunderstorms Over CHQ', 'DUE TO TS OVER CHQ WE ARE DIVERTING TO HER'),
                            ('Aircraft Diverting to IAH Due to Fuel Considerations', 'DIVERTING TO IAH NOW. WE DID NOT HAVE THE FUEL TO HLD'),
                            ('Small Bird Strike Reported on Climb Out', 'SMALL BIRD STRIKE ALL OK JUST INFORMING'),
                            ('Bird strike, no damage', 'BIRD STRIKE NO DAMAGE FOUND ENG CHECKED'),
                            ('Aircraft Holding at ORD, Possible Diversion to MKE', 'HOLDING IN ORD MAY DIVERT TO MKE'),
                            ('Diversion planned', 'WE PLAN ON DIVERTING TO RSW DUE ENG INDICATION'),
                            ('Lengthy tarmac delay at KORD', 'LENGTHY TARMAC DELAY OFF GATE FOR 150 MINS'),
                            ('Holding over Frankfurt', 'HOLDING DUE TRAFFIC'),
                            ('A birthday wish from the cockpit', 'ITS OUR FO BIRTHDAY'),
                            ('Back to the gate', 'RETURNING TO GATE DUE MX'),
                            ('A smell in the galley', 'SMELL SEEMS TO BE GONE'),
                            ('Deviating around weather', 'DEVIATING NORTH AROUND WEATHER')]:
            self.assertEqual(story_tier(title, text, 70), 'notable', title)

    def test_routine_operations_are_background(self):
        for title, text in [('UA4199/04 Experiences Lengthy Tarmac Delay at KORD',
                             'LENGTHY TARMAC DELAY OFF GATE FOR 60 MINS. FLIGHT MUST BE AIRBORNE OR RETURNING TO GATE OR EGRESS'),
                            ('APU Inoperative, No Thrust Reverser Credit', 'APU INOP'),
                            ('Turbulence Warning for Sydney Airport Runway 34R', 'POSSIBLE TURBULENCE ON FINAL'),
                            ('Aircraft back in service', 'ACFT RETURNED TO SERVICE AFTER MX'),
                            ('Ground power requested', 'NEED GPU UPON ARRIVAL'),
                            ('', '')]:
            self.assertEqual(story_tier(title, text, 70), 'background', title)

    def test_low_rated_serious_wording_is_not_major(self):
        self.assertEqual(story_tier('Smoke reported', 'SMOKE IN CABIN', 40), 'notable')
        self.assertEqual(story_tier('Smoke reported', 'SMOKE IN CABIN', 60), 'major')

    def test_negated_and_conditional_actions_do_not_count(self):
        for text in ('NOT DIVERTING, CONTINUING TO DEST', 'IF DIVERTING ADVISE OPS',
                     'NO LONGER DIVERTING', 'UNABLE TO RETURN TO KIAH'):
            self.assertEqual(story_tier('Crew update', text, 70), 'background', text)

    def test_tier_follows_current_rules_not_stored_value(self):
        self.assertEqual(with_tier(dict(title='Holding', tier='major', interestScore=90))['tier'], 'notable')
        self.assertEqual(with_tier(dict(title='Smoke in the cabin', transmission='SMOKE', interestScore=70))['tier'], 'major')

    def test_tier_outranks_interest(self):
        major = dict(tier='major', interestScore=60, receivedAt='2026-10-04T10:00:00Z', id='a')
        notable = dict(tier='notable', interestScore=95, receivedAt='2026-10-04T11:00:00Z', id='b')
        background = dict(tier='background', interestScore=100, receivedAt='2026-10-04T12:00:00Z', id='c')
        ranked = sorted([background, notable, major], key=importance_key, reverse=True)
        self.assertEqual([story['id'] for story in ranked], ['a', 'b', 'c'])

    def test_fresher_story_wins_within_a_tier_but_never_across_tiers(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)
        older = dict(tier='notable', interestScore=75, receivedAt='2026-10-02T18:00:00Z', id='old')
        today = dict(tier='notable', interestScore=70, receivedAt='2026-10-04T09:00:00Z', id='new')
        major = dict(tier='major', interestScore=60, receivedAt='2026-10-02T17:00:00Z', id='major')
        ranked = sorted([older, major, today], key=lambda s: importance_key(s, now), reverse=True)
        self.assertEqual([story['id'] for story in ranked], ['major', 'new', 'old'])

    def test_top_keeps_one_story_per_aircraft_and_mixes_categories(self):
        def s(key, registration, category, tier='notable'):
            return dict(id=key, registration=registration, category=category, tier=tier)
        stories = [s('1', 'N4199', 'Operations'), s('2', 'N-4199', 'Operations'), s('3', 'N1', 'Diversion'),
                   s('4', 'N2', 'Diversion'), s('5', 'N3', 'Weather'), s('6', 'N4', 'Diversion'),
                   s('7', 'N5', 'Operations', tier='background')]
        unique = one_per_aircraft(stories)
        self.assertEqual([story['id'] for story in unique], ['1', '3', '4', '5', '6', '7'])
        # Never two of a category in a row while another waits in the same tier;
        # a lower tier never moves up.
        self.assertEqual([story['id'] for story in diversify(unique)], ['1', '3', '5', '4', '6', '7'])

    def test_alert_floor_by_tier(self):
        self.assertTrue(alert_worthy(dict(tier='major', interestScore=60)))
        self.assertFalse(alert_worthy(dict(tier='major', interestScore=59)))
        self.assertTrue(alert_worthy(dict(tier='notable', interestScore=65)))
        self.assertFalse(alert_worthy(dict(tier='notable', interestScore=64)))
        self.assertFalse(alert_worthy(dict(tier='background', interestScore=100)))
        self.assertFalse(alert_worthy(dict(tier='major', interestScore='90')))


if __name__ == '__main__':
    unittest.main()
