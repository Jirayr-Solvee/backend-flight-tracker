# Backend Operations Notes

## Sofly AWS Backend

- Production API: `https://api.sofly.to`
- AWS region: `eu-north-1`
- EC2 instance: `sofly-server` / `i-050789b7e55614f51`
- Public IP: `56.228.47.127`
- SSH user: `ubuntu`
- Server repo path: `/home/ubuntu/backend-flight-tracker`
- Main backend service: `flight-tracker.service`
- Fetcher service: `flight-fetcher.service`
- Nginx proxies `api.sofly.to` to `127.0.0.1:8000`
- CloudWatch currently only has the incoming email Lambda log group; FastAPI backend logs are local systemd journal/nginx logs on EC2.

### Connect To EC2

Use EC2 Instance Connect with a temporary local key. Do not store or print private keys.

```bash
tmpdir=$(mktemp -d)
ssh-keygen -t ed25519 -N '' -f "$tmpdir/sofly-eic" -q
aws ec2-instance-connect send-ssh-public-key \
  --region eu-north-1 \
  --instance-id i-050789b7e55614f51 \
  --availability-zone eu-north-1b \
  --instance-os-user ubuntu \
  --ssh-public-key "file://$tmpdir/sofly-eic.pub"

ssh -o StrictHostKeyChecking=accept-new \
  -i "$tmpdir/sofly-eic" \
  ubuntu@56.228.47.127
```

### Check Logs

```bash
sudo journalctl -u flight-tracker.service --since "2 hours ago" --no-pager
sudo journalctl -u flight-tracker.service --since "2 hours ago" --no-pager \
  | egrep -i "live-positions|resolve|unable|aerodatabox| 500 | 404 |Exception|Traceback|error"

sudo tail -n 1000 /var/log/nginx/access.log | egrep -i "live-positions|/flights"
sudo systemctl status flight-tracker.service --no-pager
sudo systemctl status flight-fetcher.service --no-pager
```

### Deploy Backend

Before deploying, commit and push local backend changes:

```bash
python3 -m compileall core
git diff --check
git status --short
git add <changed-files>
git commit -m "<message>"
git push origin main
```

Then connect to EC2 and deploy:

```bash
cd /home/ubuntu/backend-flight-tracker
git status --short
git pull --ff-only origin main
/home/ubuntu/backend-flight-tracker/venv/bin/python3 -m compileall core
sudo systemctl restart flight-fetcher.service && \
  /home/ubuntu/backend-flight-tracker/venv/bin/python3 scripts/check_service_readiness.py --service fetcher && \
  sudo systemctl restart flight-tracker.service && \
  /home/ubuntu/backend-flight-tracker/venv/bin/python3 scripts/check_service_readiness.py && \
  systemctl is-active flight-fetcher.service flight-tracker.service
sudo journalctl -u flight-tracker.service --since "2 minutes ago" --no-pager | tail -n 80
```

- Restart the services sequentially, not simultaneously. Each readiness gate has a 45-second total deadline and a 3-second per-request timeout. It requires two consecutive HTTP 200 OpenAPI responses with the expected service-specific route, without authentication or provider calls.
- A systemd unit can be `active` while a Gunicorn worker is stalled before startup. Never call deployment healthy based only on `systemctl is-active`, an open TCP port, or one successful request to the other service.
- If either gate fails, stop the deployment sequence and inspect both service journals/processes. The checker does not automatically restart or roll back services. Do not continue to the next restart or report success.
- This proves application HTTP readiness, not third-party provider health, database behavior, or every API worker. Follow it with the scoped authenticated smoke check relevant to the change; do not print credentials, raw query text, or response bodies.

### Test Global Flights

The global flight endpoints require user auth. For backend-only verification, create a temporary guest user on the server using `settings.GUEST_KEY`; do not print the token.

Useful production checks:

- `GET /flights/live-positions?limit=70` should return `200`.
- Globe candidates should avoid suffix-style callsigns like `RYR84LH`, `THY4KH`, `QTR29C`, because Aerodatabox often cannot resolve them.
- Resolve a sample of visible candidates with `/flights/live-positions/resolve?callsign=...&icao24=...`.
- Check that failures are not `500`; expected untrackable misses usually show `404 {"detail":"Unable to resolve live flight"}`.

Recent known-good deploy:

- `3ee96cc Tighten South America global flight candidates`
- After deploy, sampled `60` candidates, `suffix_style_count=0`, `JJ` candidates removed, and first `15/15` sampled candidates resolved with `200`.
