# Changelog

What changed for people who use the service, one section per release. The version
lives only in `pyproject.toml`; each release is a Git tag `vX.Y.Z` and a container
image `ghcr.io/normangw/entity-resolver:X.Y.Z`. Versions follow
[semantic versioning](https://semver.org/): a breaking change to the API bumps the
first number (the second while we are below 1.0), a new feature the second, a fix the
third.

## Unreleased

### Changed
- `/health` and `/docs` report the version from `pyproject.toml`. The `APP_VERSION`
  setting is gone; remove it from your `.env` (it is now ignored).
- The full alias index lives outside the image, in the `alias-index` volume at
  `/data/index`, and `ALIAS_INDEX_PATH` comes from `.env`. Without a `.env` the app
  serves the bundled 5-company seed. Servers that used the old method (index copied
  into `src/` before the build) must run `scripts/load_index.sh` once and set
  `ALIAS_INDEX_PATH=/data/index/companies.full.json` in `.env`.

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

## v0.1.0 (2026-11-12)

Everything on `main` before Deliverable 3: the walking skeleton testers used in
Homework 3, plus the hardening that followed their findings. Tag `v0.1.0` marks commit
`9031f63`.

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

