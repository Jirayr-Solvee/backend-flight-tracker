# Apple Ads measurement

Sofly owns this measurement path so CAC and ROAS do not depend on an AppsFlyer
ROI360 upgrade.

## Data contract

- The release iOS app obtains an AdServices token once per Sofly install. The
  backend exchanges it with Apple inside the 24-hour token window and stores
  only the decoded campaign, ad-group, and keyword identifiers. The token is
  never logged or persisted.
- Apple Ads Campaign Management API reports provide daily spend and delivery
  metrics. Ad-group totals are the non-overlapping source for campaign and
  ad-group reporting. Search Match keyword spend is derived as ad-group spend
  minus explicit-keyword spend.
- Verified StoreKit JWS transactions and App Store Server Notifications provide
  trials, purchases, renewals, prices, currencies, and refunds. Production
  reporting excludes Xcode and Sandbox transactions.
- Reports join acquisition to revenue by Sofly's authenticated user and stable
  device/install IDs. Simulator and Debug builds never submit attribution.
- Revenue currencies are converted to the row's spend currency using the latest
  available ECB reference rates. Both original and normalized values are
  returned; ROAS remains null when a required rate is unavailable.

The AdServices join starts prospectively with the first app build containing the
reporter. Historic Apple Ads spend can be imported, but Apple does not provide a
way to recreate expired per-install AdServices attribution tokens.

## Apple Ads read-only API setup

Apple Ads requires a dedicated user with the `API Read Only` role. An Account
Admin cannot use its existing role as the API user. Invite a separate Apple
Account under Account Settings > User Management, accept the invitation with
that account, and upload an EC P-256 public key from its API settings.

Store the corresponding private key only on the backend host and configure:

```text
APPLE_ADS_CLIENT_ID=SEARCHADS...
APPLE_ADS_TEAM_ID=SEARCHADS...
APPLE_ADS_KEY_ID=...
APPLE_ADS_PRIVATE_KEY_PATH=/etc/sofly/apple-ads-private-key.pem
APPLE_ADS_ORG_ID=...
```

Never commit the private key or print these credentials in logs.

## Operations

Protected endpoints use the existing backend operations bearer token:

- `GET /apple-ads/status`
- `POST /apple-ads/spend/sync`
- `POST /apple-ads/revenue/backfill`
- `GET /apple-ads/report?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&dimension=campaign`

The report dimension can be `campaign`, `ad_group`, or `keyword`. It returns
cost per attributed install, cost per verified trial, paying-customer CAC, and
D0/D7/D30 ROAS with maturity counts.

For a daily server job, run:

```bash
/home/ubuntu/backend-flight-tracker/venv/bin/python \
  /home/ubuntu/backend-flight-tracker/scripts/sync_apple_ads.py
```
