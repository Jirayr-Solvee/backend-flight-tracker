from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


PRIVACY_POLICY_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sofly Privacy Policy</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.6;
      margin: 0;
      color: #13202f;
      background: #f7fbff;
    }
    main {
      max-width: 760px;
      margin: 0 auto;
      padding: 48px 20px 64px;
      background: #ffffff;
    }
    h1, h2 {
      line-height: 1.25;
      color: #0b1726;
    }
    h1 {
      margin-top: 0;
    }
    a {
      color: #0a67c8;
    }
  </style>
</head>
<body>
  <main>
    <h1>Sofly Privacy Policy</h1>
    <p>Effective date: June 23, 2026</p>

    <p>
      Sofly: Track Flight Status helps people search, save, and track flight
      information. This policy explains what information Sofly processes and
      how it is used.
    </p>

    <h2>Information we process</h2>
    <p>
      Sofly may process account details you provide, Apple sign-in identifiers,
      saved flight data, device identifiers for notifications, app diagnostics,
      subscription status from Apple, and advertising attribution information
      from partners such as AppsFlyer and Meta.
    </p>

    <h2>How we use information</h2>
    <p>
      We use information to provide flight tracking, sync saved flights, deliver
      notifications, manage subscriptions, respond to support requests, improve
      the app, measure campaign performance, and prevent misuse of the service.
    </p>

    <h2>Sharing</h2>
    <p>
      Sofly does not sell personal information. We share information only with
      service providers needed to run the app, including hosting, analytics,
      attribution, notification, and payment providers, or when required by law.
    </p>

    <h2>Data deletion</h2>
    <p>
      You can request deletion of your Sofly account and associated personal
      data by contacting <a href="mailto:info@sofly.to">info@sofly.to</a>.
      We may retain limited records when required for security, legal,
      accounting, or fraud-prevention reasons.
    </p>

    <h2>Contact</h2>
    <p>
      Questions about this policy can be sent to
      <a href="mailto:info@sofly.to">info@sofly.to</a>.
    </p>
  </main>
</body>
</html>
"""


SUPPORT_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sofly Support</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.6;
      margin: 0;
      color: #13202f;
      background: #f7fbff;
    }
    main {
      max-width: 760px;
      margin: 0 auto;
      padding: 48px 20px 64px;
      background: #ffffff;
    }
    h1, h2 {
      line-height: 1.25;
      color: #0b1726;
    }
    h1 {
      margin-top: 0;
    }
    a {
      color: #0a67c8;
    }
  </style>
</head>
<body>
  <main>
    <h1>Sofly Support</h1>
    <p>
      Need help with Sofly: Flight Tracker &amp; Radar? Contact us at
      <a href="mailto:track@sofly.to">track@sofly.to</a>. Please include your
      flight number and departure date when asking about a flight search or
      tracking issue.
    </p>

    <h2>Flight search and tracking</h2>
    <p>
      Search using an airline flight number and departure date, or enter the
      departure and arrival airports. Gate, terminal, baggage, status, and delay
      details appear when they are available from the flight-data provider.
    </p>

    <h2>Subscriptions</h2>
    <p>
      You can manage or cancel your subscription in iOS Settings under your
      Apple Account and Subscriptions. Use Restore Purchases in Sofly if an
      active subscription is not recognized.
    </p>

    <h2>Privacy</h2>
    <p>
      Read the <a href="/privacy.html">Sofly Privacy Policy</a> or contact us at
      <a href="mailto:info@sofly.to">info@sofly.to</a> for privacy questions and
      account-deletion requests.
    </p>
  </main>
</body>
</html>
"""


@router.api_route("/privacy.html", methods=["GET", "HEAD"], response_class=HTMLResponse)
def privacy_html() -> str:
    return PRIVACY_POLICY_HTML


@router.api_route("/privacy", methods=["GET", "HEAD"], response_class=HTMLResponse)
def privacy() -> str:
    return PRIVACY_POLICY_HTML


@router.api_route("/support", methods=["GET", "HEAD"], response_class=HTMLResponse)
def support() -> str:
    return SUPPORT_HTML


@router.api_route(
    "/support.html", methods=["GET", "HEAD"], response_class=HTMLResponse
)
def support_html() -> str:
    return SUPPORT_HTML
