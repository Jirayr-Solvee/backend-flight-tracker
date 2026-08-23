import unittest

from core.routers.legal import (
    LUMA_PRIVACY_POLICY_HTML,
    PRIVACY_POLICY_HTML,
    SUPPORT_HTML,
    router,
)


class LegalPageTests(unittest.TestCase):
    def test_privacy_policy_has_contact_and_deletion_information(self):
        self.assertIn("Sofly Privacy Policy", PRIVACY_POLICY_HTML)
        self.assertIn("Data deletion", PRIVACY_POLICY_HTML)
        self.assertIn("mailto:info@sofly.to", PRIVACY_POLICY_HTML)

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
            "/support",
            "/support.html",
        ):
            self.assertIn(path, routes)
            self.assertEqual(routes[path], {"GET", "HEAD"})


if __name__ == "__main__":
    unittest.main()
