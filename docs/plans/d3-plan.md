# Plan: meet the Deliverable 3 checklist

## Context
Deliverable 3 ("Final Project, Deliverable Part III-2.pdf") is worth 20 points: 10 for the software, 10 for the presentation. It asks for a service that installs the same way everywhere, ships and rolls back with one command, keeps strangers out of other people's data, and degrades clearly. The audit found the core is there: Pydantic Settings, uv.lock, a non-root Dockerfile, compose with healthchecks, retries with a breaker, request ids, `/metrics`, a backup script, and a postmortem. Missing: release and deploy automation, HTTPS, loguru, Prometheus and Grafana, a runbook, an SLO, several docs, `discussion.pdf`, and replies to the tester issues. Decisions so far: HTTPS on the Jetstream DNS name, and Caddy replaces nginx.

What I found in the code and how it changes the plan:
- **The index has to move out of the image.** Today the full index gets into the `alias-index` volume by copying it into `src/` before `docker compose build`. Once deploys pull a prebuilt image from GHCR, that image only carries the 5-company seed. So the index moves to its own volume path and is loaded or restored there. This also makes "swapping the model is a .env change and a restart" true.
- **Progress is already shown.** The upload page polls `/status` with a progress bar (`static/index.html:423-429`), which meets "show progress in some other way". No SSE needed; just document it.
- **Ownership needs work.** Upload jobs are keyed by `uuid4` (`uploads.py:180`) with no owner check. The rubric says to check ownership on the server for every object, so add a cheap cookie check.
- **Env files are incomplete.** `.env.production.example` lacks 30 of the 37 settings, and `.env.example` lacks `BATCH_BUDGET_SECONDS`.
- **429 handling conflicts with the rubric.** The Wikidata client never retries a 429. The rubric says to retry "a 429 after its Retry-After", so this needs a small change and an update to CLAUDE.md's decision.
- **Logging and metrics need replacing.** Logging uses a stdlib `JsonFormatter` (`observability.py:26`), and metrics are a hand-rolled renderer with one global latency histogram.

Process for every work item: open an issue from `.github/ISSUE_TEMPLATE`, use a branch named for the issue, open a PR with "Closes #n", get a teammate's review, and squash merge. When a branch is stale, cherry-pick its commits onto a fresh branch. Run CI locally first (ruff check and format, mypy, `PYTHONPATH=src uv run pytest`). **Teammates must own `src/` changes.** The rubric checks that every member has application-code commits, and today the other members have 2-7 commits each. Workstreams C-G are good to hand off.

## Phase 0: today (unblocks everything)
- Run `gitleaks detect` over the full history. If it finds a real key, rotate it now.
- Find the instance's DNS name (`<instance>.<project>.projects.jetstream-cloud.org`). Open ports 443 and 80 in the Jetstream security group.
- Confirm `main` holds #97 and close the stale branch `ops-retry-backup-postmortem`.
- Add a `.mailmap` so Norman / normangw / normanwgy count as one person.
- Create one issue per workstream below. Check the course's generative-AI policy and disclose AI help where the policy asks for it.

## Owners
Teammates were assigned at random. Names go on the issues in Phase 0, each with a due date.

| Workstream | Owner |
|---|---|
| A (CI, registry, release), B (index volume, deploy/rollback), C (Caddy, HTTPS, ownership, auth), E (settings, resilience) | Norman |
| D (observability: loguru, metrics, Prometheus, Grafana, SLO) | Hongjin Zeng |
| F (docstrings, ruff D1) | pangy8869898 |
| H (tester feedback) | Ken Qin |

Norman does A → B → C → E in that order; Phase 2 needs all four. D, F and H run in parallel and don't block Phase 2.

Every member needs application-code commits. H is mostly docs and GitHub comments, so **Ken Qin also implements the tester fixes H picks** (in `src/`, for example the home-page coverage text in `static/index.html` and the language/canonical-name issue), each as its own PR. D and F already land in `src/`.

## Workstream A: CI, registry, release (owner: Norman)
- `ci.yml`: on push and on PRs, run `uv sync --frozen`, ruff, mypy and pytest, then build the image. On PRs, build only. On `main`: push `ghcr.io/<owner>/entity-resolver:<sha>` and `:main`, then smoke-test the pushed image.
  - The smoke test runs the image with `ENVIRONMENT=development`, so the production validator (needs `API_KEY`) doesn't block it.
  - Steps: wait for `/health` and `/ready`, then run `eval/smoke-seed.jsonl` (seed-index cases only: Lyft, gibberish, a non-company).
  - Check that `/health` reports the version from `pyproject.toml`. That proves `importlib.metadata` works inside the image; the Dockerfile's second `uv sync --frozen --no-dev` installs the project, so metadata is present.
- `release.yml` on `v*` tags: retag `<sha>` as `<version>`, then create a GitHub Release from that version's CHANGELOG section.
- Version source of truth is `pyproject.toml` only. `config.py` `app_version` defaults to `importlib.metadata.version("entity-resolver")`. Add a unit test plus the in-image check above.
- Rewrite `CHANGELOG.md` (it is still the "appdeploy" template) with user-facing entries per version. Tag `v0.2.0` once A and B are merged.
- Pin every action in `ci.yml`, `uptime.yml` and the new workflows by commit SHA, with a `# vX` comment. Pin `python:3.12-slim-bookworm` and the uv image by digest. Each later image gets pinned in the PR that adds it.
- Add a gitleaks job to CI and gitleaks to `.pre-commit-config.yaml`.
- Make the GHCR package public, since the repo is public; that's one setting. Otherwise the runbook needs a `docker login` step with a read-only token.

## Workstream B: index on a volume, deploy and rollback, known-good check (owner: Norman)
- **Index volume:** in `compose.yml`, mount `alias-index` at `/data/index`. Read `ALIAS_INDEX_PATH` from `.env` instead of hardcoding it under `environment`. Dev default: the bundled seed, so a fresh clone works with `docker compose up -d`. Production: `/data/index/companies.full.json`.
- New `scripts/load_index.sh`: copies a local index or a backup archive into the volume using a one-off container and `scripts/backup_index.py restore`. This replaces the `down -v` dance in CLAUDE.md.
- **`scripts/deploy.sh <version>`:** run `docker compose pull`. Record the digest (`docker image inspect --format '{{index .RepoDigests 0}}'`). Run `up -d`, wait until healthy (with a timeout), then run the known-good check. Append `timestamp version digest result` to `~/entity-resolver-deploy.log`. If the check fails, exit non-zero and print the rollback command.
- **Rollback:** the same script with the previous version. `deploy.sh --previous` reads the version from the log. `compose.yml` keeps a `build:` section for local dev and uses `image: ghcr.io/...:${VERSION:-main}`.
- **Known-good sets, in two files:**
  - `eval/smoke-seed.jsonl`: works with the 5-company seed; used by CI.
  - `eval/smoke.jsonl`: about 15 fixed queries that need the full index and Wikidata (Lyft; YouTube resolving with Google as parent; a typo; a non-English alias; same-name cases; gibberish that must stay unresolved; a non-company). Used by `deploy.sh` on Jetstream.
  - Extend `scripts/monitor.py` (or `evaluate.py`) with `--smoke <file>` so it exits non-zero on any mismatch. Also add the junk, same-name and non-company queries to `eval/queries.jsonl` and re-run `scripts/evaluate.py` (open item in TASKS.md).

## Workstream C: Caddy, HTTPS, caching, auth (owner: Norman)
- Add a `Caddyfile` with the site address from `{$SITE_ADDRESS}` and `reverse_proxy api:8000`. Port the settings from `nginx.conf`:
  - security headers plus HSTS;
  - `request_body max_size` at 12MB for `/upload` and 512KB for the JSON routes;
  - transport timeouts: dial 5s, read 120s;
  - `/metrics` → `respond 404`.
- Stock Caddy has no rate limiter, so the app's `protection.py` limits (429 + `Retry-After`) are the limiter. Say so in the docs.
- `compose.yml`: replace the `proxy` service. Publish only 80/tcp and 443/tcp. Turn HTTP/3 off with the global option `servers { protocols h1 h2 }`, so no UDP port or extra security-group rule is needed. Add named volumes `caddy-data` and `caddy-config`, a healthcheck, and `restart: unless-stopped`. Delete `nginx.conf` and update README and CLAUDE.md.
- Let's Encrypt fallback: if issuance hits a rate limit, use Caddy's staging CA (`acme_ca`) to keep demoing. Get the certificate early in Phase 2.
- **Cache-Control** (app middleware, tested): `/` (the HTML page, which changes each deploy) → `no-cache`; every JSON and download response → `no-store`. If CSS/JS is split into `/static/` with hashed names, serve those with `max-age=31536000, immutable`; otherwise document that there are no separate static assets.
- **Ownership** (`main.py` `_job()` at line 416, `uploads.py` `Job`):
  - On `POST /upload`, create the job with a random owner token. Store only its SHA-256 on the `Job`, and set the token as an `HttpOnly; Secure; SameSite=Strict` cookie with `Path=/upload`. Every job route (`/upload`, `/upload/{id}/preview`, `/resolve`, `/status`, `/download`; checked in `main.py`) sits under that path.
  - `_job()` compares tokens with `hmac.compare_digest`. On a mismatch or missing token it returns **404** (not 403, so it doesn't confirm the id exists).
  - Tests use `TestClient(app, base_url="https://testserver")` so the `Secure` cookie is sent back. They check that user B (a second client) cannot preview, start a resolve, read the status of, or download user A's job, and that user A can do all four.
  - Make `Secure` conditional on `environment == "production"`, so a plain-HTTP local `docker compose up` still works.
  - API clients keep the cookie with `curl -c/-b`; document that in the README.
- **Auth:** `/metrics` and `/resolve/batch` require the key. Have `protection.api_key` accept both `X-API-Key` and `Authorization: Bearer`, so Prometheus can authenticate with `credentials_file`. The production profile requires `API_KEY` (see E). Test that the routes return 401 with no key and with a wrong key.

## Workstream D: observability (owner: Hongjin Zeng)
- **loguru:** add the dependency. Rewrite the logging setup in `observability.py` around a loguru `serialize`-style JSON sink, with an `InterceptHandler` that routes uvicorn and stdlib logs into loguru.
  - Keep `request_id_var`, so every line from one request carries the same id, and keep the response header.
  - Fields: request id, method, route template, status, duration_ms, `source` (local / wikidata / gleif) and `scorer`. Never log the name, filename or answer.
  - Extend the existing privacy tests in `tests/test_product.py`.
- **Metrics:** switch to `prometheus_client`; it replaces the hand-rolled renderer. Metrics:
  - per-route request counter and latency histogram, labeled with the route template;
  - an error counter;
  - quality numbers: resolutions by `source`/`status` (gives the unresolved rate), a Wikidata call-duration histogram, breaker opens, and the cache hit ratio.
  - Keep the label sets fixed and small.
- **Compose:** add `prometheus` and `grafana`, pinned by digest, with named volumes, healthchecks and `restart: unless-stopped`.
  - Prometheus stays unpublished.
  - Grafana binds to `127.0.0.1:3000:3000`, so it is reachable only through `ssh -L 3000:localhost:3000`. Set an admin password from `.env`.
  - The runbook notes that `docker ps` lists `127.0.0.1:3000`, which is loopback only. From outside, only 80 and 443 are open; prove it with `ss -tlnp` or `nmap` from the Mac.
  - `ops/prometheus.yml` scrapes `api:8000/metrics` with the bearer token from a file.
  - `ops/grafana/` holds a provisioned datasource and one dashboard: request rate, error rate, p95 latency, unresolved rate, Wikidata latency, SLO burn.
- **SLO** (`docs/slo.md`), for `POST /resolve`:
  - A good request is any non-5xx answer, served within 1s from the local index or within 6s on the Wikidata path. Base the bounds on measured p99s from `scripts/loadtest.py`.
  - Target: 99% over 28 days, so the error budget is about 1%. Include the PromQL and the dashboard panel.

## Workstream E: settings, resilience, swappability (owner: Norman)
- **Env files:** make `.env.example` (development) and `.env.production.example` (deployment) list every setting, with placeholders for secrets. Add a test that compares the keys in both files against `Settings.model_fields`, so they can't drift.
- **Validators:**
  - Add `environment: Literal["development","production"]`.
  - A `model_validator` fails at startup when production has no `API_KEY` or doesn't use JSON logs, or when `gleif_low_threshold >= match_threshold`.
  - Add tests for each case.
- **Swap the model by .env:** add a `SCORER` setting (`ratio`, `WRatio`, `token_set_ratio`), passed to `Resolver(scorer=...)` (`resolver.py:189`). Add `WIKIDATA_API_URL` too. With `ALIAS_INDEX_PATH` from B, any model change is an `.env` edit and a restart.
- **429 after Retry-After** (`wikidata.py`, around line 106):
  - If Wikidata's `Retry-After` is at most 2s and fits the remaining retry budget, sleep with jitter and retry once.
  - Otherwise return 503 and pass a `Retry-After` header to our caller.
  - Every 503 the app returns gets `Retry-After`.
  - **Interaction with the breaker and timeouts:** a 429 followed by its retry counts as one failure toward the breaker, never two.
  - Worst case for one `/resolve`: queue wait 5s, plus the Wikidata call, plus at most one 2s wait, plus the retry call. Cap the whole lookup with an overall deadline, so the total stays under the SLO's Wikidata bound and under the proxy's 120s read timeout. Set the SLO bound from this computed worst case and a measured p99, not from a guess.
  - Tests with the existing fake fetcher: 429 with `Retry-After: 1` → one retry, then success; 429 with `Retry-After: 30` → 503 with `Retry-After`; repeated 429s → the breaker opens after N lookups, not 2N; the deadline holds.
  - Update the CLAUDE.md decision and the tests.
- **Repeated writes:** `POST /upload/{id}/resolve` while a run is active → 409. A second run redoes only the `retry` rows. Document both behaviors and test them.
- **Time limits table:** every outbound call and its timeout, each justified by the measured slowest normal request: Wikidata 10s, GLEIF 5s, queue 5s, batch budget 60s, Caddy 120s, uvicorn graceful shutdown 20s, `deploy.sh` and `monitor.py` HTTP timeouts. It goes into the architecture doc.
- **Dockerfile and image:** drop `!.env.example` from `.dockerignore`, so no env file of any kind is in the image.
- **Upload job format:** make `load_job` tolerate unknown fields and missing fields with defaults, so the old and new versions can read each other's job files during a rollback. That's the expand/contract story. Jobs expire after 1h, so no numbered migrations are needed; document why.

## Workstream F: docstrings and style (owner: pangy8869898)
- Add docstrings to the 63 definitions that lack them in `src/`. Enable ruff `D1` for `src/` and ignore it under `tests/` via per-file-ignores, so CI enforces it. One consistent docstring style throughout, for the "stylistically consistent" rubric line.

## Phase 2: one Jetstream session (Norman; after A, B, C, E are merged)
Paste the real output of each step into the runbook as you go:
1. Fresh install from the README on the instance. Set `SITE_ADDRESS`, then run `deploy.sh v0.2.0` and confirm the HTTPS certificate with `curl -vI https://<host>/health`. `docker ps` should show only 80 and 443 published.
2. Load the full index: `scripts/load_index.sh data/companies.full.json`. `/ready` should report ready and the smoke check should pass.
3. **Backup:** run `scripts/backup_index.py backup`, which is safe because the index file is read-only at runtime. Copy the archive off the instance (scp to the Mac and a teammate's drive). Uploads are deliberately not backed up (1h TTL, user-owned, can be redone); explain why.
4. **Restore drill:** wipe the volume, restore from the off-instance copy, and confirm the smoke check passes.
5. **Rollback drill:** deploy a new version, then run `deploy.sh --previous` and confirm `/health` shows the old `build`.
6. **Reboot drill:** run `sudo reboot`. Check what came back by itself (`docker compose ps`, HTTPS, `/ready`) and what didn't. Confirm `systemctl is-enabled docker`.
7. **Failure drills, recorded:** Wikidata blocked → clear 503 with `Retry-After` and `retry` rows; 130 requests a minute → 429; an 11MB file → 413; `/metrics` from outside → 404; another browser's job → 404.
8. Set the `APP_URL` repository variable to `https://<host>` so `uptime.yml` runs.
Also time `scripts/build_index.py` for the README's rebuild-time line.

## Workstream G: docs (split; after Phase 2)
- `docs/runbook.md`: fresh install, deploy, rollback, backup, restore, reboot, load or swap the index, rotate the API key, and the BuildKit cache fix. Each section gives the exact commands and the expected output from Phase 2.
- `docs/architecture.md`: revised process, execution, state and technical diagrams, written in Mermaid and exported to SVG/PNG. They show Caddy, the api, Prometheus, Grafana, GHCR, every volume, the backup path off the instance, Wikidata and GLEIF, and where each time limit sits. Mark it as superseding `docs/deliverable2/`.
- `docs/scaling.md`: what breaks with two copies behind one address. All of these live in one process's memory: the rate limiter buckets, the Wikidata TTL cache, the circuit breaker, the concurrency semaphore, the GLEIF daily budget, and the running upload jobs (their files sit on a ReadWriteOnce volume). Fixes: Redis for the limiter, cache, breaker and budget; object storage plus a job queue and worker for uploads. Then a sketch of how Kubernetes would host it (Deployment, Service, Ingress with cert-manager, PVC or object storage, readiness/liveness probes on `/ready` and `/health`, a CronJob for backups). Description only, no implementation.
- `docs/security.md`:
  - A threat summary: who can reach what, auth, ownership, limits, and secrets in neither the repo, the image nor the logs.
  - The gitleaks result and a pinning table.
  - **The LLM statement:** the app has no language model. It reads untrusted uploads only through RapidFuzz, and its only outbound calls (Wikidata, GLEIF) carry company names, so nothing can send private data out. Discussed with the team on <date>.
  - A migrations note: no database.
- `docs/postmortem-*.md`: keep the tester-findings postmortem. Add a second one if a real incident happens in Phase 2 (a failed certificate, the BuildKit corruption); otherwise one is enough.
- **README top:** every team member's full name, a one-paragraph aim, and a three-step demo (web page, curl `/resolve`, spreadsheet). A curl command for every endpoint, checked against `/docs`. Data section: where the index comes from, `build_index.py`, rebuild time, the volume. Links to the runbook, architecture, SLO, security and scaling docs.

## Workstream H: tester feedback (owner: Ken Qin; start early)
- `docs/tester-responses.md` lists all ~50 open issues, of three kinds (14 "Design question", 5 "Question", 3 "D2", and the rest are bugs or UX). For each: a fix with a PR link, won't fix with a reason, or an answer.
  - Several already have answers in the docs from commit 32211af (#43, #59, #61, #73, #88-92). Link them.
  - Check issues that may already be fixed (for example "Wikidata errors show up as 'Model still loading'", which `index.html:309` now handles).
  - Fix a few on purpose and explain the choice. Candidates: #84 (Time Warner index data), the language setting changing the canonical name, and the home page not explaining its coverage.
- Then post a comment on each issue and close it. These go to GitHub publicly, so the team decides who posts and when.

## Phase 4: analysis, presentation, final ship
- `discussion.pdf`: at most 3 pages, Times New Roman 12pt, single-spaced, 1-inch margins. Rewrite `discussion.md` (it currently holds the HW2 audit; move that to `docs/`). Content:
  - the user and the problem (messy supplier and company names);
  - the architecture;
  - baseline versus improvements: `WRatio` at 0.80 accepted "instagram" as Norfolk Southern, so `ratio` at 0.90 with about 4.9% of typos missed; the Wikidata fallback; tie-breaks; from `eval/README.md`;
  - tester findings and what we did;
  - lessons from deployment.
  Build the PDF from Word or Google Docs (or pandoc) and check the page count.
- **Slides**, 10 points, a story: the user's pain, a wrong answer at 100% confidence, testers finding it, how we measured and fixed it, hardening, a live demo, lessons. The live demo follows a fixed script (single name, brand to parent, gibberish, spreadsheet with progress, a rate-limit 429, Grafana). Keep a recorded backup video in case the instance or Wi-Fi fails.
- **Ship:** tag `v1.0.0` with CI green on that commit, then `deploy.sh v1.0.0`. Refresh `USER_BRIEF.md`, the report and `APP_URL` last. Unshelve the instance well before the final lecture and run the smoke check that morning. Submit the slides, repo link, app URL and `discussion.pdf`.

## Order and parallelism
Phase 0 → (A → B → C → E: Norman, in sequence) ‖ (D: Hongjin, F: pangy, H: Ken, in parallel) → Phase 2 (needs A, B, C, E) → G → Phase 4.

Norman's chain is long. If it slips, E's smaller items (`SCORER`, `WIKIDATA_API_URL`, the job-format tolerance) can move after Phase 2. Only the env files and the production validator have to come before it. D and F don't block Phase 2. If time runs short, cut in this order (lowest value first): the second postmortem, the Grafana basic-auth route (use the SSH tunnel), the `WIKIDATA_API_URL` setting, extra Grafana panels. Don't cut HTTPS, release and deploy scripts, the runbook drills, loguru, Prometheus and Grafana, tester responses, or `discussion.pdf`. Each is a separate rubric line.

## Verification (rubric line → evidence)
- **Install:** a fresh clone, `uv sync --frozen` and `docker compose up -d` work with no manual input. CI is green on the submitted tag, including the image build and smoke test.
- **Tests:** pytest covers endpoints through TestClient, settings rejecting bad values (including the production profile), the Wikidata fake, `/health` during a slow request (exists, `tests/test_main.py:128`), ownership 404, auth 401, 429 with `Retry-After`, 413, Cache-Control headers, env-file drift and the version.
- **Docstrings:** ruff `D1` is clean.
- **Network:** `docker ps` on the instance publishes only 80 and 443; `curl -vI https://<host>` shows a valid certificate.
- **Ship and roll back:** `deploy.sh` and `deploy.sh --previous` both leave a line in the deploy log and change the `/health` build.
- **Operations:** the reboot, restore and failure drills are recorded in the runbook with real output.
- **Monitoring:** the Prometheus target is up, Grafana shows `loadtest.py` traffic, and a grep of the logs finds no names or filenames.
- **Tester issues:** every one has a linked response and is closed.
- **Analysis:** `discussion.pdf` is at most 3 pages in the required format.
