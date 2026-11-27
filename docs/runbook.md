# Runbook

How to run the service on the Jetstream2 instance. Every section gives the exact
commands and what you should see. Run them from the repository checkout on the
instance (`~/entity-resolver`) unless a step says "from your laptop".

Under each section, **Drill result** records what happened when we ran it on the
live instance on 2026-11-27 (`team-4-final`, Ubuntu 24.04, m3.small with a 60 GB
volume-backed root disk). `docs/drills.md` is the script for those drills.

Related: [architecture](architecture.md), [time limits](time-limits.md),
[security](security.md), [drills](drills.md), [postmortem: fresh install](postmortem-fresh-install.md).

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
   `[::]` forms); the api shows only `8000/tcp` (not published). Grafana shows
   `127.0.0.1:3000->3000/tcp`, which is loopback only. Prometheus is not published.

**Drill result (2026-11-27).** A fresh install on a new instance, because the old
one's 20 GB root disk was 95% full. Use a 60 GB root disk (Exosphere: *Custom disk
size (volume-backed)*); the four images, build cache and logs outgrow 20 GB.

- Docker and Compose were already on the Featured-Ubuntu24 image
  (`Docker Compose version 2.40.3`), and `docker` is enabled at boot.
- Ports 80 and 443 were already open; no security-group change was needed.
- The GHCR package is public: `deploy.sh` pulled `0.2.0` without `docker login`.
- **Found a bug:** `scripts/load_index.sh` failed with
  `Permission denied: '/data/index/.companies.full.json.tmp'`. The image had no
  `/data/index`, so the new volume was root-owned. Fixed in #126 (the image creates
  it and CI checks it). A volume created before #126 needs a one-time
  `docker run --rm --user root -v entity-resolver_alias-index:/data/index --entrypoint chown ghcr.io/normangw/entity-resolver:0.2.0 -R 1000:1000 /data/index`
  (`docker compose run` cannot: compose drops the `CHOWN` capability).
- Caddy's first certificate order failed with `HTTP 404 ... Certificate not found`
  while downloading the chain; it retried by itself and logged
  `certificate obtained successfully` about 75 s after start. Until then HTTPS
  answers with a TLS `internal error`. Wait a minute before testing.

Step 6, from a laptop:
```
$ curl -s https://team-4-final.cis240466.projects.jetstream-cloud.org/health
{"status":"ok","version":"0.1.0","build":"083ce6c"}
$ openssl s_client ... | openssl x509 -noout -issuer -enddate
issuer=C = US, O = Let's Encrypt, CN = YE1
notAfter=Feb 25 20:09:44 2027 GMT
$ curl -sI http://team-4-final.cis240466.projects.jetstream-cloud.org/health | head -1
HTTP/1.1 308 Permanent Redirect
```
`/metrics` from outside answers `404`; `/resolve/batch` without the key `401`.

Step 7, with the observability profile on:
```
entity-resolver-grafana-1       127.0.0.1:3000->3000/tcp
entity-resolver-prometheus-1    9090/tcp
entity-resolver-api-1           8000/tcp
entity-resolver-proxy-1         0.0.0.0:80->80/tcp, [::]:80->80/tcp, 0.0.0.0:443->443/tcp, [::]:443->443/tcp, 443/udp, 2019/tcp
guacamole-exo-guac-guacamole-1  0.0.0.0:49528->8080/tcp, [::]:49528->8080/tcp
guacamole-exo-guac-guacd-1      4822/tcp
```
`443/udp` and `2019/tcp` are ports the Caddy image declares, not published ones.
The `guacamole-exo-guac` containers are Exosphere's web shell, not part of the
app; it answers `404` on 49528. Remove it with Exosphere's web-desktop setting off,
or stop it with `docker compose -p guacamole-exo-guac down` if the rubric's "only
80 and 443" must hold strictly.

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

**Drill result (2026-11-27).** `.env` from `.env.example` plus
`ENVIRONMENT=production`, `LOG_FORMAT=json`, `API_KEY`, `SITE_ADDRESS`,
`ALIAS_INDEX_PATH=/data/index/companies.full.json` and
`COMPOSE_PROFILES=observability`. `scripts/deploy.sh 0.2.0`:
```
== pull ghcr.io/normangw/entity-resolver:0.2.0
digest ghcr.io/normangw/entity-resolver@sha256:87b55d3a5f1148081bca3f2ac50fba250e9f32d5f75cc439ba24dd551fdbedf8
== start
 ✔ Container entity-resolver-api-1         Healthy    7.2s
== wait for healthy and ready (up to 180s)
== known-good answers (eval/smoke.jsonl)
info: version 0.2.0, build e488871
deployed 0.2.0 (ghcr.io/normangw/entity-resolver@sha256:87b55d3a...), result WARN
$ curl -s https://team-4-final.cis240466.projects.jetstream-cloud.org/health
{"status":"ok","version":"0.2.0","build":"e488871"}
```
The `WARN` was Wikidata throttling, not the release: we ran four deploys within a
minute, and every second one hit Wikidata's burst limit (five Wikidata cases `503`,
all local answers correct). The same check run alone a few minutes later passed
cleanly (`ok`, 17 answers, p95 755 ms). Leave a minute or two between deploys.

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

**Drill result (2026-11-27).** Deployed the previous `main` commit, then `0.2.0`,
then `scripts/deploy.sh --previous`, then `0.2.0` again:
```
rolling back to 944c2be41ac374df5a91b32a93a5f6b681e929b7 (from /home/exouser/entity-resolver-deploy.log)
info: version 0.1.0, build 944c2be
$ cat ~/entity-resolver-deploy.log
2026-11-27T21:42:33Z 944c2be41ac374df5a91b32a93a5f6b681e929b7 ghcr.io/normangw/entity-resolver@sha256:12ea9d31... WARN
2026-11-27T21:42:56Z 0.2.0 ghcr.io/normangw/entity-resolver@sha256:87b55d3a... WARN
2026-11-27T21:43:18Z 944c2be41ac374df5a91b32a93a5f6b681e929b7 ghcr.io/normangw/entity-resolver@sha256:12ea9d31... WARN
2026-11-27T21:43:38Z 0.2.0 ghcr.io/normangw/entity-resolver@sha256:87b55d3a... WARN
```
Each deploy or rollback took about 20 s. The index, uploads, certificates and
Prometheus data stayed in their volumes throughout.

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

**Drill result (2026-11-27).** On the instance, with 0.2.0 running:
```
$ docker compose run --rm --no-deps -T -u "$(id -u):$(id -g)" -v "$PWD/scripts:/scripts:ro" \
    -v "$PWD/backups:/out" --entrypoint python api /scripts/backup_index.py backup /data/index/companies.full.json /out
wrote /out/alias-index-20261027.tar.gz
$ ls -l backups/
-rw-r--r-- 1 exouser exouser 934290 Nov 27 21:54 alias-index-20261027.tar.gz
```
From the laptop:
```
$ scp 'exouser@149.165.149.202:entity-resolver/backups/alias-index-*.tar.gz' ~/entity-resolver-backups/
alias-index-20261027.tar.gz      100%  912KB   2.7MB/s   00:00
$ python3 scripts/backup_index.py verify ~/entity-resolver-backups/alias-index-*.tar.gz
{
  "index_sha256": "f137279f1e2ba611872b8f60e5933fa0907d5a253f9de95bc661b9faff2d4084",
  "companies": 14127,
  "built_at": "20261027"
}
```
The archive is named after the index build date, not the backup date, so this
copy has the same name and size as the one restored in section 6.

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
docker compose up -d                   # the api restarts in a loop: .env points at a missing index
scripts/load_index.sh backups/alias-index-<date>.tar.gz
docker compose up -d                   # now healthy; start the proxy if it was waiting
```

The api refusing to start without its index is deliberate: it never silently
serves the 5-company seed in production.

**Drill result (2026-11-27).** Restored onto the brand-new instance from the copy
kept off the instance (on a laptop, made with `backup_index.py backup` on
2026-11-12):
```
$ scp backups/alias-index-20261027.tar.gz exouser@149.165.149.202:~/backups/    # from the laptop
$ scripts/load_index.sh ~/backups/alias-index-20261027.tar.gz
loading /home/exouser/backups/alias-index-20261027.tar.gz into the alias-index volume
restored /data/index/companies.full.json
restarting api to load the new index
done; check readiness with: curl -s http://127.0.0.1/ready
$ curl -s localhost/ready
{"status":"ready"}
$ curl -s -X POST localhost/resolve -H 'Content-Type: application/json' -d '{"company_name":"Lyft"}'
{"input":"Lyft","status":"resolved","wikidata_id":"Q17077936",...,"source":"local","confidence":1.0,...}
$ curl -s -X POST localhost/resolve -H 'Content-Type: application/json' -d '{"company_name":"YouTube"}'
{"input":"YouTube","status":"resolved","wikidata_id":"Q866",...,"parent_company":"Google",...,"source":"wikidata",...}
```
The first attempt failed with `Permission denied` (see the fresh-install drill
result and #126). As designed, the api refused to start while `.env` pointed at
the still-empty volume.

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

**Drill result (2026-11-27).** `sudo reboot` with 0.2.0 and the observability
profile running; nothing was started by hand:
```
$ uptime
 21:50:50 up 4 min,  2 users,  load average: 0.23, 0.27, 0.14
$ systemctl is-enabled docker
enabled
$ docker compose ps
entity-resolver-api-1          ghcr.io/normangw/entity-resolver:0.2.0   Up 4 minutes (healthy)
entity-resolver-grafana-1      grafana/grafana:12.4@sha256:982f...      Up 4 minutes (healthy)
entity-resolver-prometheus-1   prom/prometheus:v3.5.0@sha256:6380...    Up 4 minutes (healthy)
entity-resolver-proxy-1        caddy:2.11.8-alpine@sha256:f622...       Up 4 minutes (healthy)
$ curl -s https://team-4-final.cis240466.projects.jetstream-cloud.org/health
{"status":"ok","version":"0.2.0","build":"e488871"}
$ python3 scripts/monitor.py https://team-4-final.cis240466.projects.jetstream-cloud.org --smoke eval/smoke.jsonl
info: version 0.2.0, build e488871
info: latency p50 134 ms, p95 226 ms over 17
ok
```
What came back: all four containers, the same release, the full index, the
certificate (from the `caddy-data` volume, no new Let's Encrypt order) and
Prometheus history. Upload jobs that were mid-run would come back as an error that
says to resolve again.

## 8. Rotate the API key

```bash
NEW=$(openssl rand -hex 32)
sed -i "s/^API_KEY=.*/API_KEY=$NEW/" .env
docker compose up -d api               # recreates the api with the new key
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1/resolve/batch \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $NEW" \
  -d '{"company_names": ["Apple"]}'    # 200
```

Give the new key to the people and scripts that use batch, and write it to the
Prometheus token file too, then restart Prometheus:
`printf '%s' "$NEW" > ../entity-resolver-observability-secrets/metrics-token && docker compose restart prometheus`. The old key stops working at once.
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
| `load_index.sh`: `Permission denied` on `/data/index` | `docker run --rm -v entity-resolver_alias-index:/d alpine ls -ld /d` | Volume created before #126 is root-owned; run the one-time `chown` in the fresh-install drill result |
| HTTPS gives a TLS `internal error` | `docker compose logs proxy \| grep -iE 'obtain\|error'` | Caddy is still getting its first certificate (about a minute); it retries by itself |
| `/ready` stays 503 | `docker compose logs api \| grep -i index` | Index missing or `ALIAS_INDEX_PATH` wrong; run section 4 |
| Many `503` with `Retry-After` | `/metrics`: `wikidata_unavailable_total`, `wikidata_busy_total` | Wikidata throttling or down. Local answers still work. Wait; the breaker retries by itself |
| `429` | `rate_limited_total` | A client over `RATE_LIMIT_*`. Expected for scripts; raise the limit in `.env` only if it is a real user |
| Upload says the file is too large | | Over 10 MB (`413`). Split the file |
