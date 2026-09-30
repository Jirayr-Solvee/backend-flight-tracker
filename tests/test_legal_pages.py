import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.routers.legal import (
    LUMA_PRIVACY_POLICY_HTML,
    LUMA_SUPPORT_HTML,
    PRIVACY_POLICY_HTML,
    SUPPORT_HTML,
    router,
)


class LegalPageTests(unittest.TestCase):
    def test_privacy_policy_has_contact_and_deletion_information(self):
        self.assertIn("Sofly Privacy Policy", PRIVACY_POLICY_HTML)
        self.assertIn("Data deletion", PRIVACY_POLICY_HTML)
        self.assertIn("mailto:info@sofly.to", PRIVACY_POLICY_HTML)

    def test_sofly_privacy_discloses_retained_sign_in_details(self):
        text = " ".join(PRIVACY_POLICY_HTML.split())
        self.assertIn("Effective date: September 28, 2026", text)
        self.assertIn("Apple sign-in identifier", text)
        self.assertIn("name and email address provided during sign-in", text)
        self.assertIn("associate these details with your Sofly account", text)

    def test_sofly_privacy_discloses_forwarded_email_storage_and_ai_processing(self):
        text = " ".join(PRIVACY_POLICY_HTML.split())
        self.assertIn("original message is stored", text)
        self.assertIn("Amazon Web Services (AWS) S3", text)
        self.assertIn("sender, recipients and subject", text)
        self.assertIn("message contents, and any attachments", text)
        self.assertIn("match the sender's email address to your Sofly account", text)
        self.assertIn("supported PDF attachments to Google's Gemini AI service", text)
        self.assertIn("may be saved to your account", text)

    def test_sofly_privacy_does_not_apply_search_expiry_to_forwarded_emails(self):
        text = " ".join(PRIVACY_POLICY_HTML.split())
        self.assertIn("not automatically deleted after processing", text)
        self.assertIn("currently have no automatic expiry", text)
        self.assertIn("sender does not match an account", text)
        self.assertIn("does not apply to forwarded emails or their attachments", text)
        self.assertIn("associated personal data, including forwarded booking emails", text)
        self.assertNotIn("not used for training", text)
        self.assertNotIn("deleted immediately", text)

    def test_sofly_privacy_distinguishes_prompt_free_search_from_booking_permission(self):
        text = " ".join(PRIVACY_POLICY_HTML.split())
        self.assertIn("without a separate AI search permission prompt", text)
        self.assertIn("Search text is not redacted before AI processing", text)
        self.assertIn("Forwarded-email AI processing remains off by default", text)
        self.assertIn("does not grant booking-import permission", text)
        self.assertIn("revoke booking-import permission in Settings", text)
        self.assertIn("cannot undo information already sent", text)
        self.assertIn("searches can work without sharing the search text", text)

    def test_sofly_privacy_explains_bounded_non_anonymous_search_diagnostics(self):
        text = " ".join(PRIVACY_POLICY_HTML.split())
        self.assertIn("phone numbers and web URLs are redacted", text)
        self.assertIn("before the sample is encrypted", text)
        self.assertIn("Redaction does not make a sample anonymous", text)
        self.assertIn("pseudonymous account identifier", text)
        self.assertIn("authenticated diagnostic report", text)
        self.assertIn("seven days after their original capture", text)
        self.assertIn("a retry does not restart this period", text)
        self.assertIn("Expired samples are excluded from reports", text)
        self.assertIn("independent cleanup process", text)

    def test_notification_copy_retention_and_provider_boundary(self):
        text = " ".join(PRIVACY_POLICY_HTML.split())
        self.assertIn("Notification-open analytics", text)
        self.assertIn("title, subtitle and message", text)
        self.assertIn("not to AppsFlyer", text)
        self.assertIn("encrypted for storage", text)
        self.assertIn("older than 90 days after receipt", text)
        self.assertIn("removed during diagnostics collection", text)
        self.assertIn("no more than seven days", text)
        self.assertIn("Account deletion also removes", text)

    def test_sofly_privacy_preserves_attribution_disclosure(self):
        text = " ".join(PRIVACY_POLICY_HTML.split())
        self.assertIn("AppsFlyer and Meta", text)
        self.assertIn("advertising attribution", text)
        self.assertNotIn("Sofly does not track", text)

    def test_sofly_public_privacy_aliases_serve_identical_updated_policy(self):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            for path in ("/privacy", "/privacy.html"):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.text, PRIVACY_POLICY_HTML)
                    self.assertTrue(
                        response.headers["content-type"].startswith("text/html")
                    )
                    head = client.head(path)
                    self.assertEqual(head.status_code, 200)
                    self.assertEqual(head.content, b"")

    def test_support_page_has_contact_subscription_and_privacy_information(self):
        self.assertIn("Sofly Support", SUPPORT_HTML)
        self.assertIn("mailto:track@sofly.to", SUPPORT_HTML)
        self.assertIn("Subscriptions", SUPPORT_HTML)
        self.assertIn('href="/privacy.html"', SUPPORT_HTML)

    def test_luma_privacy_policy_has_consent_and_deletion_information(self):
        self.assertIn("Luma Tales", LUMA_PRIVACY_POLICY_HTML)
        self.assertIn("Advertising personalization remains disabled", LUMA_PRIVACY_POLICY_HTML)
        self.assertIn("request deletion", LUMA_PRIVACY_POLICY_HTML)
        self.assertIn("mailto:jirayr.melikyan.jm@gmail.com", LUMA_PRIVACY_POLICY_HTML)

    def test_luma_support_has_purchase_privacy_and_contact_information(self):
        self.assertIn("Luma Tales Support", LUMA_SUPPORT_HTML)
        self.assertIn("one-time consumable video packs", LUMA_SUPPORT_HTML)
        self.assertIn('href="/luma/privacy"', LUMA_SUPPORT_HTML)
        self.assertIn("mailto:jirayr.melikyan.jm@gmail.com", LUMA_SUPPORT_HTML)

    def test_public_legal_routes_support_get_and_head(self):
        routes = {
            route.path: route.methods
            for route in router.routes
            if hasattr(route, "methods")
        }
        for path in (
            "/privacy",
            "/privacy.html",
            "/luma/privacy",
            "/luma/privacy.html",
            "/luma/support",
            "/luma/support.html",
            "/support",
            "/support.html",
        ):
            self.assertIn(path, routes)
            self.assertEqual(routes[path], {"GET", "HEAD"})


if __name__ == "__main__":
    unittest.main()
