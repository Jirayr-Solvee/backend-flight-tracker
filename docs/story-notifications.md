# Aircraft-story notifications

The worker is disabled by default. Production enablement requires `SOFLY_STORY_PUSH_ENABLED=1`, `SOFLY_STORY_PUSH_VERSION=3.9.3`, and `SOFLY_STORY_PUSH_BUILD=142` in `/etc/sofly/story-push.env`. Bump the exact pair only when a newer supported release is ready. Unknown/older/newer-unconfigured builds, Debug, TestFlight, disabled permissions/preferences and registrations older than 30 days are excluded. Existing flight alerts are unchanged.

The app refreshes its device capability/version, language, time zone and permission/preference on launch/foreground and preference changes. A legacy registration clears capability. Settings allow turning story pushes off independently of flight alerts. Device metadata is removed with account deletion.

Target three/day, not a guarantee. The worker checks every ten minutes and sends in local windows 10:00–11:59, 15:00–16:59, 19:00–20:59. No catch-up outside those windows. Three-hour minimum spacing, one reservation per slot per user, and maximum three in any rolling 24 hours apply across devices. Cached stories must be under 24 hours old, from a recently refreshed feed, marked notification-eligible, and score at least 80. Existing safe-publication/translation review rules remain mandatory. No extra AI calls and no filler. Repeated normalized transmissions are suppressed for 90 days.

SQLite `BEGIN IMMEDIATE` reserves before sending, across independent workers. A failed, timed-out or interrupted attempt consumes its reservation; uncertain sends are not retried by our scheduler. This is not a guarantee of APNs delivery or device rendering. `accepted` means APNs returned 200. The APNs notification ID correlates with the existing encrypted notification-open analytics. The story-detail `experience_action` records the message ID and successful presentation separately.

Use the installed ten-minute timer only after an exact-build TestFlight test of cold/background taps and language/preference behavior. Never switch the normal worker to TestFlight or development. Single-device release QA is separate, explicitly scoped, and must not broadcast.

`GET /cockpit/stories/{id}` requires authentication and reads only retained stories (seven days). The push contains an opaque story ID, no invented flight ID. If a message has expired or the service is unavailable, the app shows a dismissible localized retry screen.

Verification: `python -m unittest tests.test_story_push tests.test_story_push_registration tests.test_cockpit_api tests.test_cockpit_ingestion`. App build/device tests are separate. Retained delivery identifiers are purged after 90 days on active worker runs and removed during account deletion; no notification text or token is logged by this worker.
