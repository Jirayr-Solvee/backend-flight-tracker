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


LUMA_PRIVACY_POLICY_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Privacy Policy - Luma Tales</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.7;
      margin: 0;
      color: #1a1a1a;
      background: #f6f5ff;
    }
    main {
      max-width: 760px;
      margin: 0 auto;
      padding: 48px 24px 64px;
      background: #ffffff;
    }
    h1, h2 {
      line-height: 1.3;
    }
    h1 {
      margin: 0 0 4px;
    }
    h2 {
      font-size: 1.2rem;
      margin-top: 34px;
    }
    .subtitle {
      color: #666;
      margin: 0 0 36px;
    }
    a {
      color: #5d3df5;
    }
  </style>
</head>
<body>
  <main>
    <h1>Privacy Policy</h1>
    <p class="subtitle">Luma Tales - Last updated: August 23, 2026</p>

    <p>Luma Tales ("we", "our", or "us") is a personalized cartoon app for parents and guardians. This privacy policy explains what information we collect, how we use it, and the choices available to you.</p>

    <h2>1. Information We Collect</h2>
    <p>When a parent or guardian creates a cartoon, the app may collect the child name or nickname, child age, selected appearance choices (such as skin tone, eye color, hair style, hair color, and outfit), selected adventure, generation prompt, generated video and audio, job status, and saved cartoon record. Previously generated story text, illustrations, and narration may remain stored for existing users.</p>
    <p>The app also uses an anonymous account identifier to save cartoons, sync the library, enforce free and paid usage limits, and keep the app working without requiring email signup.</p>
    <p>For subscriptions, we process subscription product identifiers, purchase status, renewal or expiration dates, and Apple transaction information needed to unlock paid features. Apple handles payment details. We do not see or store credit card numbers.</p>
    <p>For analytics and attribution, we may collect app opens, paywall views, subscription events, cartoon generation status, selected template identifiers, product interaction events, device or installation identifiers, and advertising attribution information. If you allow tracking on iOS, Apple's advertising identifier may be used to measure which ads brought you to Luma Tales.</p>

    <h2>2. How We Use Information</h2>
    <p>We use cartoon inputs to generate personalized videos and sound; save cartoons in your library; enforce usage limits; provide subscriptions; improve app reliability; understand app performance; and measure the effectiveness of our advertising campaigns.</p>
    <p>We do not send child names, generation prompts, or generated videos to advertising analytics providers. These details are used to provide the cartoon experience.</p>

    <h2>3. Third-Party Services</h2>
    <p>We use third-party services to operate the app:</p>
    <ul>
      <li><strong>Supabase</strong> for anonymous authentication, backend infrastructure, private video storage, usage limits, and cartoon records.</li>
      <li><strong>Google Gemini API</strong> for cartoon video and audio generation.</li>
      <li><strong>OpenAI, Google Gemini, or ElevenLabs</strong> for previously supported story, image, or narration generation when applicable to legacy records.</li>
      <li><strong>Apple StoreKit</strong> for in-app purchases and subscriptions.</li>
      <li><strong>AppsFlyer</strong> for app analytics, attribution, and advertising measurement.</li>
    </ul>
    <p>These providers process data as needed to provide their services to us. Analytics and attribution events sent to AppsFlyer do not include child names, generation prompts, generated videos, story text, illustrations, or narration audio.</p>

    <h2>4. Tracking and Advertising Measurement</h2>
    <p>Luma Tales does not display third-party ads in the app. We may use AppsFlyer to understand which marketing campaigns lead to installs, subscriptions, and app engagement.</p>
    <p>Before AppsFlyer begins advertising measurement, Luma Tales asks whether the parent or guardian allows advertising measurement. We send the resulting consent signal to AppsFlyer so advertising partners such as Google can honor that choice. Advertising personalization remains disabled. When consent is required by applicable law, advertising data usage and advertising identifier storage are based on that consent.</p>
    <p>On iOS, tracking that uses Apple's advertising identifier is also controlled by Apple's App Tracking Transparency prompt. The in-app privacy choice and Apple's system permission are separate controls. You can deny either without losing access to the cartoon experience.</p>

    <h2>5. Data Storage and Retention</h2>
    <p>Cartoon records are saved locally on your device and on our backend so your library can load reliably and usage limits can be enforced. Generated videos are stored in private cloud storage and accessed through temporary signed links. We keep this information for as long as needed to provide the app unless you request deletion.</p>

    <h2>6. Children's Privacy</h2>
    <p>Luma Tales is intended for use by parents and guardians on behalf of children. We do not knowingly collect personal information directly from children under 13. Parents and guardians control the information entered into the app and should avoid entering sensitive personal information.</p>

    <h2>7. Your Choices</h2>
    <p>You can review or change the advertising measurement choice from the Privacy Choices button in Luma Tales. A new choice is applied to future AppsFlyer events. You can separately control Apple's tracking permission in iOS Settings under Privacy &amp; Security &gt; Tracking.</p>
    <p>You can manage subscriptions through your Apple ID settings. You can request deletion of backend data associated with your anonymous account by contacting us.</p>

    <h2>8. Data Sharing</h2>
    <p>We do not sell personal information. We share information with service providers only as needed to operate the app, generate cartoons, process subscriptions, measure app performance, and measure advertising effectiveness as described in this policy.</p>

    <h2>9. Security</h2>
    <p>We use reasonable technical and organizational measures to protect information processed by the app. No system is completely secure, but we work to limit access and protect family cartoon data.</p>

    <h2>10. Changes to This Policy</h2>
    <p>We may update this policy from time to time. When we do, we will update the date at the top of this page.</p>

    <h2>11. Contact Us</h2>
    <p>If you have questions or deletion requests, contact us at:<br>
    <a href="mailto:jirayr.melikyan.jm@gmail.com">jirayr.melikyan.jm@gmail.com</a></p>
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


@router.api_route(
    "/luma/privacy.html", methods=["GET", "HEAD"], response_class=HTMLResponse
)
def luma_privacy_html() -> str:
    return LUMA_PRIVACY_POLICY_HTML


@router.api_route(
    "/luma/privacy", methods=["GET", "HEAD"], response_class=HTMLResponse
)
def luma_privacy() -> str:
    return LUMA_PRIVACY_POLICY_HTML


@router.api_route("/support", methods=["GET", "HEAD"], response_class=HTMLResponse)
def support() -> str:
    return SUPPORT_HTML


@router.api_route(
    "/support.html", methods=["GET", "HEAD"], response_class=HTMLResponse
)
def support_html() -> str:
    return SUPPORT_HTML
