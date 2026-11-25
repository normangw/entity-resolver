# Changelog

What changed for people who use the service, one section per release. The version
lives only in `pyproject.toml`; each release is a Git tag `vX.Y.Z` and a container
image `ghcr.io/normangw/entity-resolver:X.Y.Z`. Versions follow
[semantic versioning](https://semver.org/): a breaking change to the API bumps the
first number (the second while we are below 1.0), a new feature the second, a fix the
third.

## Unreleased

Nothing yet.

## v0.2.0 (2026-11-25)

Deliverable 3: the first release deployed from a published image. Upgrading from
`main` before this release: copy `.env.production.example` to `.env`, set `API_KEY`
and `SITE_ADDRESS`, run `scripts/load_index.sh` once, then `scripts/deploy.sh 0.2.0`
(see `docs/runbook.md`).

### Changed
- `/health` and `/docs` report the version from `pyproject.toml`. The `APP_VERSION`
  setting is gone; remove it from your `.env` (it is now ignored).
- The full alias index lives outside the image, in the `alias-index` volume at
  `/data/index`, and `ALIAS_INDEX_PATH` comes from `.env`. Without a `.env` the app
  serves the bundled 5-company seed. Servers that used the old method (index copied
  into `src/` before the build) must run `scripts/load_index.sh` once and set
  `ALIAS_INDEX_PATH=/data/index/companies.full.json` in `.env`.
- Caddy replaces nginx as the reverse proxy. With `SITE_ADDRESS` set to the server's
  DNS name the service is served over HTTPS (Let's Encrypt) and port 80 redirects to
  it; only ports 80 and 443 are published. Open 443 in the firewall.
- An uploaded file belongs to the browser that uploaded it: other browsers, and
  requests without the `upload_owner` cookie, get `404` on its preview, status,
  resolve and download. Scripts must keep the cookie (`curl -c jar -b jar`).
- Responses carry `Cache-Control`: `no-cache` for the page, `no-store` for everything
  else.
- A Wikidata `429` with a short `Retry-After` (2 s or less) is waited out once per
  lookup instead of failing at once. Each lookup has a 15 s deadline
  (`WIKIDATA_DEADLINE_SECONDS`). Every `503` now carries `Retry-After`, and a
  Wikidata `429` passes Wikidata's value on.
- Two starts of the same upload job sent at once: one gets `202`, the other `409`.
- The web page gives up on a request after a time limit and says so, instead of
  waiting forever.
- `.env.example` is no longer copied into the image.
- An upload job saved by another version of the app (extra or missing fields) still
  loads after a deploy or rollback.

### Breaking
- New setting `ENVIRONMENT`. With `ENVIRONMENT=production` the app refuses to start
  without an `API_KEY` of at least 16 characters and `LOG_FORMAT=json`. Copy the new
  `.env.production.example` and set `API_KEY`.
- With `GLEIF_ENABLED=true`, `GLEIF_LOW_THRESHOLD` must be below `MATCH_THRESHOLD`.

### Added
- Every push and pull request builds the container image and smoke-tests it against a
  fixed set of known answers. Commits on `main` are published to the GitHub Container
  Registry as `ghcr.io/normangw/entity-resolver:<commit>`; tags publish
  `:<version>`.
- Secret scanning (gitleaks) over the whole Git history in CI and before each commit.
- `scripts/deploy.sh <version>` deploys a published image, waits until it is healthy,
  checks known-good answers (`eval/smoke.jsonl`) and logs the result;
  `deploy.sh --previous` rolls back to the last good version.
- `scripts/load_index.sh` loads an index file or a backup archive into the volume,
  after the same checks as a restore.
- The API key can also be sent as `Authorization: Bearer <key>`, so Prometheus can
  scrape `/metrics`.
- Settings `SCORER` (swap the RapidFuzz scorer with a `.env` edit), `WIKIDATA_API_URL`,
  `WIKIDATA_DEADLINE_SECONDS` and `WIKIDATA_MAX_RETRY_AFTER`.
- `docs/time-limits.md`: every timeout, where it is set and why.
- Operator docs: `docs/runbook.md`, `docs/architecture.md` (supersedes
  `docs/deliverable2/`), `docs/scaling.md`, `docs/security.md`, `docs/drills.md`.
- The evaluation covers junk input, same-name companies and non-companies, and can
  run through the service path (`scripts/evaluate.py --service`); see
  `eval/README.md`.

### Fixed
- With `language` set to `zh`, `ja`, `de`, `fr` or `es`, a Wikidata match returns its
  translated name in `label` (it was always empty).
- Saving an upload job's answers no longer blocks other requests while it writes.

## v0.1.0 (2026-11-12)

Everything on `main` before Deliverable 3: the walking skeleton testers used in
Homework 3, plus the hardening that followed their findings. It was never tagged:
there is no `v0.1.0` tag (the only tags are `blocking-resolve` and `hw2-submission`),
and the state it describes is commit `9031f63`. It predates the release pipeline, so
there is no `ghcr.io/normangw/entity-resolver:0.1.0` image and `scripts/deploy.sh`
cannot deploy or roll back to it. The first deployable release is v0.2.0.

### Added
- `POST /resolve`: a company name in, the canonical name, Wikidata id, parent company,
  headquarters and country out, from a RapidFuzz scorer (`ratio`, threshold 0.90) over
  an alias index of about 14,000 listed companies built from Wikidata.
- Wikidata fallback for brands and subsidiaries not in the index (#28).
- Spreadsheet upload: resolve a company-name column of an .xlsx, .xls or .csv file
  (#27).
- A web page at `/`, `/health`, `/ready`, Docker and Compose setup, settings from the
  environment.
- nginx reverse proxy: the only published port is 80; per-client rate limits, a body
  size cap and security headers (#71).
- `POST /resolve/batch` for up to 100 names, an optional API key (`X-API-Key`),
  per-client rate limits that answer `429` with `Retry-After`, a JSON body size cap,
  `/metrics`, JSON logs with a request id (`X-Request-ID`), and `BUILD_ID` in `/health`
  (#95).
- Same-name companies are tie-broken (top-level before subsidiary), flagged
  `needs_review`, and some carry a note; answers show `matched_alias` and `as_of`
  (#67, #94).
- An uploaded file over 10 MB gets a readable message instead of an nginx error page
  (#96).
- Index backup, verify and restore (`scripts/backup_index.py`) and an uptime check every
  30 minutes (`scripts/monitor.py`) (#97).

### Changed
- Wikidata retries live only in the Wikidata client (backoff, jitter, circuit breaker).
  Upload and batch rows whose lookup fails are marked `retry`, and a re-run redoes only
  those rows (#97).
- The canonical name is always English; the requested language comes back as `label`.
  An unknown `language` is rejected with `422` (#69).

### Fixed
- Confident wrong matches such as "instagram" resolving to Norfolk Southern Railway
  (#33, #34, #35, #67).
- Uploads: GBK and UTF-16 files, other delimiters, header rows, result columns that
  clashed with input columns, and jobs that never expired (#68).

