import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from core.services.cockpit_stories import curate_message, save_messages, read_stories, utc_string

NOW = datetime(2026, 9, 25, 20, tzinfo=timezone.utc)

def message(text='NEED GPU UPON ARRIVAL APU INOP', **values):
    return dict(timestamp=utc_string(NOW - timedelta(hours=1)), text=text, tail='OE-TEST', flightNumber='EC1234', **values)


class CockpitStoryTests(unittest.TestCase):
    def test_curates_specific_claim_without_inventing_emergency(self):
        result = curate_message(message(), NOW)
        self.assertEqual(result['category'], 'Operations')
        self.assertIn('requested', result['summary'])
        self.assertNotIn('emergency', result['title'].lower())

    def test_automated_diagnostics_and_negated_diversions_not_stories(self):
        self.assertIsNone(curate_message(message('ENGINE VIBRATION 0.2 N2'), NOW))
        self.assertIsNone(curate_message(message('NOT DIVERTING TO EGNX DUE FUEL'), NOW))

    def test_names_and_contacts_outside_excerpt_not_published(self):
        result = curate_message(message('DISPATCHER NAME: PRIVATE PERSON +1 202 555 0199 NEED GPU UPON ARRIVAL APU INOP'), NOW)
        self.assertEqual(result['transmission'], 'NEED GPU UPON ARRIVAL APU INOP')
        self.assertNotIn('PRIVATE', str(result))

    def test_unknown_words_inside_excerpt_rejected(self):
        self.assertIsNone(curate_message(message('FWD GALLY HAD A SMELL PRIVATE PERSON OVEN OFF SMELL GONE'), NOW))

    def test_old_future_and_naive_timestamps_rejected(self):
        for timestamp in [utc_string(NOW-timedelta(days=8)), utc_string(NOW+timedelta(hours=1)), '2026-09-25T10:00:00', 'invalid']:
            row=message(); row['timestamp']=timestamp
            self.assertIsNone(curate_message(row, NOW))

    def test_only_message_position_accepted(self):
        result=curate_message(message(flight={'latitude': 30, 'longitude': 40}), NOW)
        self.assertIsNone(result['latitude'])
        result=curate_message(message(latitude=float('nan'), longitude=40), NOW)
        self.assertIsNone(result['latitude'])
        result=curate_message(message(latitude=30, longitude=40), NOW)
        self.assertEqual(result['latitude'], 30)

    def test_cache_deduplicates_expires_and_filters_exact_registration(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'messages.db'
            self.assertIsNone(read_stories(path, now=NOW))
            self.assertEqual(save_messages(path,[message(), message()],NOW),1)
            self.assertEqual(len(read_stories(path, 'oe-test', NOW)['stories']),1)
            self.assertEqual(read_stories(path, 'OE-OTHER', NOW)['stories'],[])
            self.assertIsNone(read_stories(path, now=NOW+timedelta(hours=3)))
            save_messages(path,[],NOW+timedelta(days=8))
            self.assertEqual(read_stories(path,now=NOW+timedelta(days=8))['stories'],[])

    def test_cabin_excerpt_preserves_uncertainty(self):
        result=curate_message(message('HI FWD GALLY HAD A SMELL WE TURMED THE OVEN OFF SMELL SEEMS TO BE GONE'), NOW)
        self.assertIsNotNone(result)
        self.assertIn('appeared',result['summary'])

if __name__ == '__main__': unittest.main()
