# Aircraft message feed operations

The app reads authenticated `GET /cockpit/stories`, optionally filtered by exact
registration. Reads never call Airframes or Gemini. Returns 20 published records
per page by default (limit 1–50) from seven days. `category` filters before paging;
`cursor` is an opaque timestamp/ID keyset boundary from `nextCursor`. Equal-time
records have deterministic ID ordering. Newer insertions do not shift subsequent
pages; refresh from the first page to see them. Retention continues to apply to
every request. A null `nextCursor` marks the end. Both global and owned-flight
endpoints support the same parameters, and recheck authorization on every page.
After two hours without successful provider ingestion,
the endpoint returns 503 instead of pretending the cache is fresh.

## Worker

`sofly-cockpit-refresh.timer` runs every ten minutes UTC. The oneshot service has
a 420-second timeout, 256 MB RAM limit, 25% of one CPU quota, low CPU priority,
read-only system/home mounts, and writes only its private state directory.
Systemd plus a file lock prevents overlapping workers.

23 targeted substring searches cover weather actions, changes of plan, human
moments, unusual cargo, cabin and ground operations. This is curated coverage,
not a complete global feed. Up to two pages per term per run; frozen query
windows and pagination cursors persist. Full pages resume next run rather than
silently advance. Initial lookback is 24 hours. Windows overlap by two minutes;
content/tail/date deduplication persists across terms and restarts.

Normally 23 requests/run, at most 46: 3,312–6,624/day (6.62%–13.25% of Pro's
50,000/day). Persisted worker limits are 100/minute, 600/hour, 8,000/day. They do
not account for unrelated consumers of the same provider key. 429 stops the run
without retries. Next run resumes without marking incomplete ingestion fresh.

## AI and safety

Existing Gemini 2.5 Flash credential. Four AI attempts per ten-minute UTC window,
at most 576 scheduled attempts/day. Themes rotate so routine operations cannot
occupy every slot. New candidates may wait; no exhaustive coverage is promised.

Before sending, reserve $0.01 against the persisted UTC monthly and daily
ledgers. Maximum monthly reservation $180, with $20 unused headroom beneath the
requested $200 feature budget. Daily allowance is $180/days-in-month. At most
10,000 input UTF-8 bytes and 1,024 output tokens, thinking disabled; at pinned
$0.30/M input and $2.50/M output prices this conservative reservation exceeds
the bounded request price. Reservations are NOT refunded, even for failures;
reserved amounts are not actual invoices. Price changes require review. This
does not cap unrelated flight-search usage on the same Gemini account.

Attempt status is written before sending. Process-interrupted attempts are not
automatically replayed. Known failed responses are marked failed; no retry burst.
AI responses require structured output, bounded strings, allowed categories,
an exact source substring, publish=true, needs_review=false and interest>=30.
Scores are editorial model estimates, not probabilities. They are retained as
interestScore for later review. Notification eligibility is false; no push
sender is present in this worker.

Obvious contact/sensitive payloads are excluded before AI. Review-held text is
private, retained no longer than seven days and not exposed in the public feed.
Heuristics and AI gates do not establish perfect privacy or factual accuracy;
review remains needed before enabling automatic pushes. No manual review UI
is included in this deployment.

## Tracked-flight coverage

The same ten-minute worker reads saved `userflightlink` flights from the existing
database in read-only mode. Multiple users share one target per aircraft. Targets
start one hour before the best known departure and remain eligible until two
hours after the arrival window (which includes 30 minutes after arrival).
Missing/invalid aircraft or UTC schedules are skipped, never invented.

Up to eight additional provider calls per run share all existing minute/hour/day
limits. Least recently attempted aircraft rotate first; high volume may defer
some targets beyond ten minutes. Use existing Mode-S identity when available,
otherwise resolve the exact registration once to an Airframes airframe ID.
Pagination is persistent. Fetching uses provider creation times; attribution uses
transmission time, exact normalized registration and matching flight number
(including airline IATA/ICAO aliases and zero padding). Missing or conflicting
flight numbers are omitted rather than assumed to belong to this leg.

Matching pending messages receive AI priority before global stories, inside the
same four-per-window and $180 reservation ceiling. Routine telemetry and sensitive
messages remain excluded. This is useful decoded coverage, not all transmissions.

`GET /cockpit/flights/{flight_id}/stories` requires the authenticated user's saved
flight link (404 for missing/unowned), derives current aircraft and schedule from
the backend, and filters before limiting to 50 stories. Missing assignment/schedule
returns explicit `awaiting_aircraft_or_schedule`. Historical same-aircraft stories
from other legs cannot appear just because the registration matches.

## State and credentials

- Root-only `/etc/sofly/cockpit.env`: Airframes and existing Gemini keys.
- Ubuntu-owned `/var/lib/sofly-cockpit/stories.sqlite`: WAL cache, cursors,
  queue, and budget ledger; service umask 0027.
- Queue bounded at 20,000 records. At capacity, page insertion/cursor update
  rolls back rather than losing the page. Seven-day retention bounds records.
- `scripts/provision_cockpit_credentials.py` explicitly runs as root, reads the
  Airframes key from stdin and Gemini from existing server dotenv, writes 0600
  credentials atomically, and never prints values.

## Check / stop

Use `systemctl status sofly-cockpit-refresh.timer` and
`journalctl -u sofly-cockpit-refresh.service` for aggregate run outcomes. Check
published, held, errors, pending and backlogged_terms. Budget exhaustion defers
AI work without starting more calls. A successful provider pass updates cache
freshness independently of AI errors; inspect job metrics as well as API health.

To stop future ingestion: `sudo systemctl disable --now sofly-cockpit-refresh.timer`.
If a worker is currently running, stopping the timer does not stop that worker;
stop its service separately when required. Existing cached records remain; they
become unavailable via API after the freshness window. Do not delete the budget
database to restart a worker: doing so erases the persisted spending safeguards.

Deploy API changes using the repository's sequential fetcher/API readiness gates.
Afterward check an authenticated feed response, unauthorized rejection, exact
registration filtering, and a bounded cached-read load sample. Do not treat
systemd-active status alone as readiness. Phone builds with recorded-message
flags continue to show those fixtures until separately rebuilt.
