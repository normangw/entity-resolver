# Drills (Phase 2 script, #106)

A step-by-step script for the one Jetstream session that proves the deployment
works. Do the steps in order. Each step says where to run it, the commands, and what
success looks like. Paste the real output into the matching
`TODO(#106)` slot in `docs/runbook.md` (or below) as you go, and note anything that
did not behave as described: that is a finding, not a failure of the drill.

Before you start:

- On your laptop: the repository, `data/companies.full.json`, and
  `export HOST=<instance>.<project>.projects.jetstream-cloud.org`.
- The release image `ghcr.io/normangw/entity-resolver:0.2.0` exists (the `v0.2.0`
  tag has been pushed and `release.yml` passed). Until then, use a commit sha that
  CI published on `main` in place of `0.2.0`.
- About two hours, and a second browser (or a private window).

Convention: **[laptop]** means your own machine, **[instance]** means an SSH session
in `~/entity-resolver` on the instance.

---

## Part A. Install and deploy

### A1. Fresh install from the README

**[instance]**

```bash
docker compose version
systemctl is-enabled docker
git clone https://github.com/normangw/entity-resolver.git && cd entity-resolver
cp .env.production.example .env && chmod 600 .env
sed -i "s/^API_KEY=.*/API_KEY=$(openssl rand -hex 32)/" .env
nano .env        # set SITE_ADDRESS to $HOST
grep -E '^(ENVIRONMENT|SITE_ADDRESS|ALIAS_INDEX_PATH)=' .env
```

Success: Compose v2 prints a version; Docker is `enabled`; `.env` shows
`ENVIRONMENT=production`, your host name and
`ALIAS_INDEX_PATH=/data/index/companies.full.json`. Security group: inbound TCP 80
and 443 open, 8000 closed.

### A2. Load the full index

**[laptop]** `scp data/companies.full.json exouser@$HOST:entity-resolver/data/`

**[instance]**

```bash
scripts/load_index.sh data/companies.full.json
```

Success: the script verifies the file and ends with
`done; check readiness with: ...`. No `note: .env does not set ALIAS_INDEX_PATH`.

### A3. Deploy

**[instance]**

```bash
scripts/deploy.sh 0.2.0
tail -1 ~/entity-resolver-deploy.log
curl -s http://127.0.0.1/health
curl -s http://127.0.0.1/ready
```

Success: `deploy.sh` exits 0; the log line ends in `ok` (or `WARN` if Wikidata was
throttling); `/health` shows `"version":"0.2.0"` and a `build` commit; `/ready` is
`{"status":"ready"}`.

### A4. HTTPS from outside

**[laptop]**

```bash
curl -vI https://$HOST/health 2>&1 | grep -E 'subject:|issuer:|SSL certificate verify|^< HTTP|strict-transport'
curl -sI http://$HOST/ | grep -iE '^(HTTP|location)'
curl -s -X POST https://$HOST/resolve -H 'Content-Type: application/json' \
  -d '{"company_name": "Wal-Mart Stores Inc."}'
```

Success: issuer is Let's Encrypt, `SSL certificate verify ok.`, `HTTP/2 200`, a
`strict-transport-security` header; plain HTTP answers `308` with
`location: https://...`; the resolve returns `"canonical_name":"Walmart"`,
`"source":"local"`. Open `https://$HOST/` in a browser: padlock, "model ready".

### A5. Only 80 and 443 are published

**[instance]**

```bash
docker ps --format 'table {{.Names}}\t{{.Ports}}'
sudo ss -tlnp | grep -vE '127.0.0.1|::1' | grep LISTEN
```

**[laptop]** `nc -zv -w 3 $HOST 8000; nc -zv -w 3 $HOST 3000`

Success: only the proxy lists published ports, `0.0.0.0:80->80/tcp` and
`0.0.0.0:443->443/tcp` (plus `[::]`); the api shows `8000/tcp` with no `->`. `ss`
lists 22, 80 and 443 on public addresses and nothing else. `nc` to 8000 and 3000
fails (timeout or refused). If #102 has landed, Grafana appears as
`127.0.0.1:3000->3000/tcp`: loopback only.

---

## Part B. Operations

### B1. Backup and copy off the instance

**[instance]**

```bash
mkdir -p backups
docker compose run --rm --no-deps -T -u "$(id -u):$(id -g)" \
  -v "$PWD/scripts:/scripts:ro" -v "$PWD/backups:/out" \
  --entrypoint python api /scripts/backup_index.py backup /data/index/companies.full.json /out
ls -l backups/
```

**[laptop]**

```bash
mkdir -p ~/entity-resolver-backups
scp "exouser@$HOST:entity-resolver/backups/alias-index-*.tar.gz" ~/entity-resolver-backups/
python scripts/backup_index.py verify ~/entity-resolver-backups/alias-index-*.tar.gz
```

Then copy the archive to a teammate's drive.

Success: one `alias-index-<build date>.tar.gz` on the instance; `verify` on the
laptop reports the checksum and company count (14,127) as OK. The service kept
answering during the backup.

### B2. Restore from the off-instance copy

**[instance]** Wipe the index volume first, so the restore starts from nothing:

```bash
docker compose down
docker volume rm entity-resolver_alias-index
rm -f backups/*.tar.gz data/companies.full.json   # only the laptop copy is left
docker compose up -d; sleep 20
docker compose ps
docker compose logs api | tail -5
```

Expected at this point: the api keeps restarting, and its log names
`alias_index_path` (`.env` points at a file that no longer exists). That is the
designed failure: the app refuses to start with the wrong index rather than quietly
serving the 5-company seed. Note what you see.

**[laptop]** `scp ~/entity-resolver-backups/alias-index-<date>.tar.gz exouser@$HOST:entity-resolver/backups/`

**[instance]**

```bash
scripts/load_index.sh backups/alias-index-<date>.tar.gz
docker compose up -d
scripts/deploy.sh 0.2.0        # re-runs the smoke check
```

Success: `load_index.sh` verifies and restores; the api comes up healthy; the deploy
log line ends in `ok`. Time the whole step; that is the restore time.

### B3. Rollback

Needs a second published version. Deploy a newer `main` commit (or `0.2.1` if one
exists), then go back.

**[instance]**

```bash
scripts/deploy.sh <newer sha or version>
curl -s http://127.0.0.1/health            # note "build"
scripts/deploy.sh --previous
curl -s http://127.0.0.1/health            # "build" is the 0.2.0 one again
tail -3 ~/entity-resolver-deploy.log
```

Success: both runs end `ok`; `/health` shows the old build after `--previous`; the
index and any finished upload are still there (the volumes were not touched).

### B4. Reboot

**[instance]** Start an upload in the browser first, then:

```bash
sudo reboot
```

Wait a minute, SSH back in:

```bash
uptime
docker compose ps
curl -s http://127.0.0.1/ready
```

**[laptop]** `curl -sI https://$HOST/health | head -1`

Success: both services are `Up ... (healthy)` without any manual command; HTTPS
answers `HTTP/2 200` with the same certificate (Caddy kept it in `caddy-data`).
The upload that was running shows an error saying to resolve again; resolving it
again reuses the answers it had. Write down anything that did not come back by
itself.

---

## Part C. Failure drills

### C1. Wikidata blocked

**[instance]** Point the app at an address that never answers (`192.0.2.1` is
reserved for documentation and never routed):

```bash
cp .env .env.bak
sed -i 's|^WIKIDATA_API_URL=.*|WIKIDATA_API_URL=http://192.0.2.1/w/api.php|' .env
docker compose up -d api && sleep 20
```

**[laptop]**

```bash
curl -si -X POST https://$HOST/resolve -H 'Content-Type: application/json' \
  -d '{"company_name": "Beats Electronics"}' | grep -iE '^(HTTP|retry-after)|detail'
curl -s -X POST https://$HOST/resolve -H 'Content-Type: application/json' \
  -d '{"company_name": "Wal-Mart Stores Inc."}' | grep -o '"status":"[a-z]*"'
```

Repeat the first command five times, then once more and time it
(`time curl ...`). Also upload a small CSV with `Beats Electronics` and `Walmart` in
the browser.

Success: the Wikidata name gets `HTTP/2 503` with a `retry-after` header and
`"Wikidata is unavailable; retry shortly"`, within about 20 s (deadline 15 s plus
one call). After five failures the breaker is open and the next `503` comes back at
once with `retry-after` up to 30. The local name still resolves. The spreadsheet
shows `retry` for Beats and `resolved` for Walmart, with the "partly done" note.

Undo: `mv .env.bak .env && docker compose up -d api`.

### C2. Too many requests (429)

**[laptop]**

```bash
for i in $(seq 130); do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST https://$HOST/resolve \
    -H 'Content-Type: application/json' -d '{"company_name": "Walmart"}'
done | sort | uniq -c
curl -si -X POST https://$HOST/resolve -H 'Content-Type: application/json' \
  -d '{"company_name": "Walmart"}' | grep -iE '^(HTTP|retry-after)'
```

Success: about 120 `200` and 10 `429` (the limit is 120 per minute per address);
the last call shows `429` and a `retry-after` header. A minute later requests pass
again.

### C3. File too large (413)

**[laptop]**

```bash
python3 -c "print('Company'); [print('Apple Inc.') for _ in range(1_100_000)]" > big.csv
ls -l big.csv                                  # about 12 MB
curl -si -F file=@big.csv https://$HOST/upload | grep -E '^HTTP|detail'
```

Also drag `big.csv` onto the web page.

Success: `413` with a JSON `detail` saying the file is too big (from the app if the
body is under 12 MB, from Caddy's JSON handler if over); the page shows the message,
not a blank error.

### C4. /metrics from outside

**[laptop]**

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://$HOST/metrics
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer <the key>" https://$HOST/metrics
```

**[instance]**

```bash
KEY=$(grep '^API_KEY=' .env | cut -d= -f2)
docker compose exec -T api python -c "import urllib.request as u; r=u.Request('http://127.0.0.1:8000/metrics', headers={'Authorization': 'Bearer $KEY'}); print(u.urlopen(r).read().decode()[:300])"
```

Success: `404` from outside, with and without the key; from inside, Prometheus text
starting with `# HELP`.

### C5. Another browser's upload job

**[laptop]**

```bash
printf 'Company\nWalmart\n' > one.csv
ID=$(curl -s -c a.txt -F file=@one.csv https://$HOST/upload | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -o /dev/null -w 'owner: %{http_code}\n'      -b a.txt "https://$HOST/upload/$ID/status"
curl -s -o /dev/null -w 'no cookie: %{http_code}\n'           "https://$HOST/upload/$ID/status"
curl -s -c b.txt -F file=@one.csv https://$HOST/upload > /dev/null
curl -s -o /dev/null -w 'other owner: %{http_code}\n' -b b.txt "https://$HOST/upload/$ID/status"
curl -s -o /dev/null -w 'made-up id: %{http_code}\n'  -b a.txt "https://$HOST/upload/0123456789abcdef0123456789abcdef/status"
```

Then in the browser: upload a file, copy the job id from the network tab, and open
`https://$HOST/upload/<id>/status` in a second browser or a private window.

Success: `owner: 200`, and `404` for no cookie, the other owner and the made-up id
(the same answer, so an id cannot be probed). The second browser gets `404`.

---

## Part D. Wrap up

1. Set the repository variable `APP_URL=https://$HOST` (Settings, Secrets and
   variables, Actions, Variables) so `uptime.yml` checks the service every 30
   minutes. Run the workflow once by hand and check it passes.
2. Time a full index build on the laptop for the README:
   `time uv run python scripts/build_index.py --out /tmp/companies.json --cache-dir /tmp/bi-cache`
   (a fresh cache directory, so nothing is reused).
3. Re-run the latency numbers on the instance for `docs/time-limits.md`:
   `python3 scripts/monitor.py https://$HOST --samples 20`.
4. Fill in every `TODO(#106)` (`grep -rn 'TODO(#106)' README.md docs/`).
5. Write a postmortem in `docs/` for anything that went wrong (a certificate
   failure, a corrupt build cache, a drill that did not match this script).

## Results

| Step | Date | Result | Notes |
|---|---|---|---|
| A1 Fresh install | 2026-11-27 | Pass, after a fix | New instance with a 60 GB disk. Found the root-owned index volume bug (#126, `docs/postmortem-fresh-install.md`). Runbook section 1 |
| A2 Load index | 2026-11-27 | Pass | `load_index.sh` from the off-instance archive, after #126. Runbook section 6 |
| A3 Deploy | 2026-11-27 | Pass | `deploy.sh 0.2.0`, about 20 s, digest recorded in the deploy log. Runbook section 2 |
| A4 HTTPS | 2026-11-27 | Pass | Let's Encrypt certificate obtained about 75 s after start; HTTP answers 308 to HTTPS. Runbook section 1 |
| A5 Only 80/443 | 2026-11-27 | Pass for the app | Only the proxy publishes 80 and 443; Grafana is on 127.0.0.1. Exosphere's web shell (`guacamole-exo-guac`) also publishes 49528, which is not part of the app (runbook section 1) |
| B1 Backup off instance | 2026-11-27 | Pass | `backup_index.py backup`, copied to a laptop with `scp`, `verify` shows 14,127 companies. Runbook section 5 |
| B2 Restore | 2026-11-27 | Pass | From the laptop copy onto the new instance; `/ready` and known answers (Lyft, YouTube) back. Runbook section 6 |
| B3 Rollback | 2026-11-27 | Pass | `deploy.sh --previous` and forward again, about 20 s each, four lines in the deploy log. Runbook section 3 |
| B4 Reboot | 2026-11-27 | Pass | All four containers healthy after `sudo reboot` with nothing started by hand; known answers p50 134 ms. Runbook section 7 |
| C1 Wikidata blocked | | Not run | Covered by tests (`tests/test_resilience.py`, `test_wikidata.py`): a 503 with `Retry-After`, local answers keep working |
| C2 429 | 2026-11-28 | Pass | 150 `POST /resolve` from one address within seconds: 124 `200`, 26 `429`; the next call answered `429` with `retry-after: 1` and `{"detail":"Too many requests. Slow down and retry shortly."}`. The limit is per client address, so a test from a pool of changing addresses never trips it |
| C3 413 | 2026-11-28 | Pass | A 17.5 MB CSV to `/upload`: `413` from Caddy with `content-type: application/json` and `{"detail":"This file is too big. The limit is 10 MB and 5,000 rows. Save it as CSV or split it."}`. An `.exe` gets `400` naming the accepted types |
| C4 /metrics outside | 2026-11-28 | Pass | `https://$HOST/metrics` answers `404`; `POST /resolve/batch` without the key `401`; an `http://` request with `Host: 127.0.0.1` from outside `403` |
| C5 Other browser's job | 2026-11-28 | Pass | Uploaded a CSV, resolved it and downloaded the result with the owner cookie; the same `/upload/{id}/status` without the cookie answers `404` |
