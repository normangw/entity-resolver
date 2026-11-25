# Runbook

How to run the service on the Jetstream2 instance. Every section gives the exact
commands and what you should see. Run them from the repository checkout on the
instance (`~/entity-resolver`) unless a step says "from your laptop".

Slots marked `TODO(#106)` get the real output from the Phase 2 session
(`docs/drills.md` is the script for it).

Related: [architecture](architecture.md), [time limits](time-limits.md),
[security](security.md), [drills](drills.md).

## At a glance

| Task | Command |
|---|---|
| Deploy a release | `scripts/deploy.sh 0.2.0` |
| Roll back | `scripts/deploy.sh --previous` |
| Load or swap the index | `scripts/load_index.sh <companies.full.json or backup .tar.gz>` |
| Back up the index | see [Back up the index](#back-up-the-index) |
| Status | `docker compose ps`, `curl -s http://127.0.0.1/ready` |
| Logs | `docker compose logs -f --since 10m api proxy` |
| Deploy history | `cat ~/entity-resolver-deploy.log` |

## 1. Fresh install

1. SSH in and install Docker if needed:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER      # then log out and back in
   sudo systemctl enable docker       # start at boot
   docker compose version
   ```
2. In Exosphere/Horizon, allow inbound TCP 80 and 443 in the instance's security
   group. Remove any old rule for 8000. Let's Encrypt checks port 80 before it
   issues the certificate.
3. Clone and configure:
   ```bash
   git clone https://github.com/normangw/entity-resolver.git
   cd entity-resolver
   cp .env.production.example .env
   chmod 600 .env
   ```
   Edit `.env`:
   - `SITE_ADDRESS=<instance>.<project>.projects.jetstream-cloud.org`
   - `API_KEY=` the output of `openssl rand -hex 32`. Production refuses to start
     without it.
4. If the GHCR package is private, log in once with a read-only token:
   `echo <token> | docker login ghcr.io -u <github user> --password-stdin`.
5. Load the full index ([section 4](#4-load-or-swap-the-index)), then deploy
   ([section 2](#2-deploy-a-release)).
6. From your laptop:
   ```bash
   export HOST=<instance>.<project>.projects.jetstream-cloud.org
   curl -vI https://$HOST/health 2>&1 | grep -E 'SSL certificate verify|HTTP/'
   ```
   Expected: `SSL certificate verify ok.` and `HTTP/2 200`.
7. Only 80 and 443 are published:
   ```bash
   docker ps --format '{{.Names}}\t{{.Ports}}'
   sudo ss -tlnp | grep -E ':(80|443|8000|3000|9090)\b'
   ```
   Expected: the proxy shows `0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp` (and the
   `[::]` forms); the api shows only `8000/tcp` (not published). Once #102 adds
   Grafana it shows `127.0.0.1:3000->3000/tcp`, which is loopback only. Prometheus
   is not published.

<!-- TODO(#106): paste real output of steps 6 and 7 -->

## 2. Deploy a release

```bash
git pull                       # the compose file and scripts that match the release
scripts/deploy.sh 0.2.0 --dry-run
scripts/deploy.sh 0.2.0
```

`deploy.sh` pulls `ghcr.io/normangw/entity-resolver:0.2.0`, records its digest,
runs `docker compose up -d`, waits up to 180 s for healthy and `/ready`, checks
`eval/smoke.jsonl` with `scripts/monitor.py`, and appends one line to
`~/entity-resolver-deploy.log`:

```
<UTC time> 0.2.0 sha256:<digest> ok
```

`ok` means every known answer matched. `WARN` means the local answers matched but
some Wikidata cases stayed `503` (throttling, by design). `failed-<step>` exits 1
and prints the rollback command.

Then check the build that is running:

```bash
curl -s http://127.0.0.1/health    # "version": "0.2.0", "build": "<commit>"
docker compose ps                  # api and proxy both "(healthy)"
```

<!-- TODO(#106): paste real output of deploy.sh and /health -->

Always deploy through the script. A bare `docker compose up -d` runs `:main`
unless `VERSION` is set.

## 3. Roll back

```bash
scripts/deploy.sh --previous
curl -s http://127.0.0.1/health    # "version" and "build" are the older ones
```

`--previous` picks the newest `ok` or `WARN` line in the deploy log whose version
differs from the last line, so it also works right after a failed deploy. The index
and uploads are in volumes, so a rollback does not touch them. Upload job files are
read by both versions (unknown fields are ignored, missing ones get defaults), so
jobs survive a rollback too.

<!-- TODO(#106): paste real output of the rollback -->

## 4. Load or swap the index

```bash
scripts/load_index.sh data/companies.full.json              # an index file
scripts/load_index.sh backups/alias-index-20261027.tar.gz   # or a backup archive
curl -s http://127.0.0.1/ready                               # {"status":"ready"}
```

The script runs `backup_index.py` in a one-off container: it checks the checksum,
the company count and that the index loads, writes
`/data/index/companies.full.json` atomically, and restarts the api. A failed load
leaves the old index in place. `.env` must contain
`ALIAS_INDEX_PATH=/data/index/companies.full.json`; the script warns if it does not.

To swap in a rebuilt index, build it on a laptop
(`uv run python scripts/build_index.py --out data/companies.full.json`, about 20
minutes), copy it up with `scp`, and run the same command. Then re-run the smoke
check: `python3 scripts/monitor.py http://127.0.0.1 --smoke eval/smoke.jsonl`
or simply `scripts/deploy.sh <current version>`.

## 5. Back up the index

The index is read-only at run time, so it can be backed up while the service runs.

```bash
mkdir -p backups
docker compose run --rm --no-deps -T -u "$(id -u):$(id -g)" \
  -v "$PWD/scripts:/scripts:ro" -v "$PWD/backups:/out" \
  --entrypoint python api /scripts/backup_index.py backup /data/index/companies.full.json /out
ls -l backups/                       # alias-index-<build date>.tar.gz
```

Then copy it off the instance. From your laptop:

```bash
scp exouser@$HOST:entity-resolver/backups/alias-index-*.tar.gz ~/entity-resolver-backups/
python scripts/backup_index.py verify ~/entity-resolver-backups/alias-index-<date>.tar.gz
```

Keep a second copy with a teammate (shared drive). What is not backed up, and why:
uploads (deleted after an hour, owned by the user who can upload again), Caddy's
certificates (Caddy gets new ones; losing them only costs a Let's Encrypt request),
logs (rotated, not a record). There is no database.

<!-- TODO(#106): paste real output of backup, scp and verify -->

## 6. Restore the index

From your laptop, copy the archive up, then on the instance:

```bash
scp ~/entity-resolver-backups/alias-index-<date>.tar.gz exouser@$HOST:entity-resolver/backups/
scripts/load_index.sh backups/alias-index-<date>.tar.gz
scripts/deploy.sh <current version>   # re-runs the smoke check
```

To prove the restore works from nothing, empty the volume first (the drill):

```bash
docker compose down
docker volume rm entity-resolver_alias-index
docker compose up -d                   # serves the seed index, /ready is 200
scripts/load_index.sh backups/alias-index-<date>.tar.gz
```

<!-- TODO(#106): paste real output of the restore drill -->

## 7. Reboot

```bash
sudo reboot
# wait about a minute, SSH back in
systemctl is-enabled docker            # enabled
docker compose ps                      # both "(healthy)" again, no manual start
curl -s http://127.0.0.1/ready
```

From your laptop: `curl -sI https://$HOST/health` gives `HTTP/2 200`. Both services
use `restart: unless-stopped`. Upload jobs that were running come back as an error
that says to resolve again; finished ones can still be downloaded until they expire.
After an unshelve the address may change: update DNS if needed and the `APP_URL`
repository variable used by `uptime.yml`.

<!-- TODO(#106): paste real output after the reboot -->

## 8. Rotate the API key

```bash
NEW=$(openssl rand -hex 32)
sed -i "s/^API_KEY=.*/API_KEY=$NEW/" .env
docker compose up -d api               # recreates the api with the new key
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1/resolve/batch \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $NEW" \
  -d '{"company_names": ["Apple"]}'    # 200
```

Give the new key to the people and scripts that use batch, and update the
Prometheus credentials file once #102 adds it. The old key stops working at once.
The key is never in the repository, the image or the logs.

## 9. Fix a broken BuildKit cache

Symptom: `docker compose build` (or `up --build`) fails with errors such as
`failed to compute cache key`, `failed to solve: ... not found` or `unexpected EOF`
on a step that has not changed. The cache is corrupt, not the code.

```bash
docker builder prune -f                 # drop the build cache only
docker compose build --no-cache api
```

On the server you should not need to build at all: `deploy.sh` pulls the image CI
built. If `docker system df` shows the disk filling up, `docker image prune -f`
removes old untagged images (keep the last two releases so a rollback does not
need a pull).

## 10. When something is wrong

| Symptom | Check | Likely cause and fix |
|---|---|---|
| `/health` fails from outside | `docker compose ps`, `docker compose logs proxy` | Proxy down, or 80/443 closed in the security group |
| Browser certificate warning | `docker compose logs proxy \| grep -i acme` | `SITE_ADDRESS` wrong, port 80 closed, or Let's Encrypt rate limit: set `ACME_CA` to the staging URL in `.env` and `docker compose up -d proxy` |
| api restarts in a loop | `docker compose logs api \| tail` | A bad `.env` value; the message names the setting (for example `API_KEY` in production) |
| `/ready` stays 503 | `docker compose logs api \| grep -i index` | Index missing or `ALIAS_INDEX_PATH` wrong; run section 4 |
| Many `503` with `Retry-After` | `/metrics`: `wikidata_unavailable_total`, `wikidata_busy_total` | Wikidata throttling or down. Local answers still work. Wait; the breaker retries by itself |
| `429` | `rate_limited_total` | A client over `RATE_LIMIT_*`. Expected for scripts; raise the limit in `.env` only if it is a real user |
| Upload says the file is too large | | Over 10 MB (`413`). Split the file |
