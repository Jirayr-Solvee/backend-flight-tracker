import unittest
from unittest.mock import patch

from scripts.send_story_notifications import supported_releases


class SupportedReleasesTests(unittest.TestCase):
    def test_accepts_exact_two_and_three_component_versions(self):
        with patch.dict('os.environ', {'SOFLY_STORY_PUSH_RELEASES': '3.9.6:145,3.10:146'}, clear=True):
            self.assertEqual(supported_releases(), {('3.9.6', 145), ('3.10', 146)})

    def test_accepts_exact_fallback_release(self):
        with patch.dict('os.environ', {'SOFLY_STORY_PUSH_VERSION': '3.10', 'SOFLY_STORY_PUSH_BUILD': '146'}, clear=True):
            self.assertEqual(supported_releases(), {('3.10', 146)})

    def test_invalid_or_missing_release_fails_closed(self):
        for value in ('', '3:146', '3.10.0.1:146', '3.10:0', '3.10:146,*:147', '3.10', '3.10:0146'):
            with self.subTest(value=value), patch.dict('os.environ', {'SOFLY_STORY_PUSH_RELEASES': value}, clear=True):
                with self.assertRaises(ValueError):
                    supported_releases()
