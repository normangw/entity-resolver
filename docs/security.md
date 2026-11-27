# Security

What can reach what, how each route is protected, where secrets live, and what is
pinned. The README's "Security review" paragraph has the code-level checks (file
names, job ids, zip bombs, formula cells); this page is the summary.

## Who can reach what

| Path | From the internet | Protection |
|---|---|---|
| Ports 80 and 443 (Caddy) | yes | Only published ports. HTTP redirects to HTTPS. TLS from Let's Encrypt. HTTP/3 (UDP) off. |
| Port 8000 (api) | no | Not published; only Caddy reaches it on the compose network. |
| Grafana 3000 *(planned, #102)* | no | Bound to `127.0.0.1`; reached through `ssh -L`. Admin password from `.env`. |
| Prometheus *(planned, #102)* | no | Not published. Scrapes `/metrics` with the API key as a bearer token. |
| SSH | yes, key only | Jetstream default (key login). |
| `GET /`, `/health`, `/ready`, `/docs` | yes | Open. `/health` shows the version and build, nothing secret. |
| `POST /resolve` | yes | Open for the demo (`API_KEY_PROTECT_ALL=true` closes it). Rate limit 120/min per client address. Body under 512 KB at Caddy and `MAX_BODY_BYTES` in the app. |
| `POST /resolve/batch` | yes | API key required when `API_KEY` is set (always in production). 10/min per address, 100 names, 60 s budget. |
| `/upload*` | yes | 20/min per address, 10 MB (12 MB at Caddy), 5,000 rows, zip-bomb limits. Each job belongs to the browser that uploaded it (below). Files deleted after 1 h. |
| `GET /metrics` | no (`404` at Caddy) | Also needs the API key in the app. |

## Authentication and ownership

- **API key.** One shared secret, sent as `X-API-Key` or `Authorization: Bearer`.
  Compared in constant time. A missing or wrong key is `401` with
  `WWW-Authenticate: Bearer` and counts in `auth_failures_total`.
  `ENVIRONMENT=production` refuses to start without a key of at least 16 characters.
- **Upload ownership.** `POST /upload` sets an `upload_owner` cookie: a random
  256-bit token, `HttpOnly`, `SameSite=Strict`, `Path=/upload`, and `Secure` over
  HTTPS. The server stores only its SHA-256 on the job and compares with
  `hmac.compare_digest`. Another browser, or a request without the cookie, gets
  `404`, the same as for an id that does not exist, so ids cannot be probed.
- **Rate limits** are per client address as Caddy saw it. Caddy overwrites
  `X-Forwarded-For`, so a client cannot fake its address.

## Secrets

| Where | Status |
|---|---|
| Repository | No secrets. `.env` is git-ignored; only `.env.example` and `.env.production.example`, with empty placeholders, are committed. gitleaks runs before each commit (pre-commit) and over the whole history in CI. |
| Image | No env file of any kind (`.dockerignore` excludes all of them; a test checks). Settings come in at run time through `env_file`. |
| Server | `.env` with `chmod 600`, in the checkout. The key is rotated with the runbook's section 8. |
| Logs | No key, company name, cell or file name is logged; access lines carry the route template, not the URL. A test checks this. |
| Metrics | Labels are route templates and fixed values only. |

**gitleaks result** (CI run on `main` at `2181223`, 2026-11-25, gitleaks v8.30.1):

```
81 commits scanned.
scanned ~5495263 bytes (5.50 MB) in 18.4s
no leaks found
```

<!-- TODO(#106): re-run on the release commit and paste the output here -->

## Pinning

| What | Pinned to | Where |
|---|---|---|
| Base image | `python:3.12-slim-bookworm@sha256:935c3c5b…` | `Dockerfile` |
| uv binary | `ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f7…` | `Dockerfile` |
| Python dependencies | exact versions and hashes | `uv.lock`, installed with `--frozen` |
| Reverse proxy | `caddy:2.11.8-alpine@sha256:f6228111…` | `compose.yml` |
| Prometheus, Grafana *(planned, #102)* | by digest | `compose.yml` |
| Our image on the server | a release version or commit sha; the digest is logged by `deploy.sh` | `~/entity-resolver-deploy.log` |
| GitHub Actions | commit sha (`actions/checkout@3d3c42e…`, `setup-python@6d4fd22…`, `setup-uv@68559e9…`) | `.github/workflows/` |
| gitleaks in CI | `ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0…` (v8.30.1) | `ci.yml` |
| pre-commit hooks | tags (`ruff-pre-commit v0.16.6`, `gitleaks v8.30.1`) | `.pre-commit-config.yaml` |

Pre-commit hooks are pinned by tag, not by commit; they run on developer machines
only, and CI runs the same checks from pinned sources.

## Untrusted input

- Company names are matched as text: normalized, then scored by RapidFuzz. They are
  sent to Wikidata and GLEIF only as URL-encoded query values, to fixed hosts, so
  there is no way to make the server call another address.
- Spreadsheets are read with size, row and decompression limits; cells that could
  run as formulas in Excel are neutralized in the output. Known gap: `openpyxl`
  parses XML without `defusedxml`, bounded by the 200 MB uncompressed limit.
- File names are used only for display and the download name, reduced to
  `[\w.\- ]`.

## Language models

The app has no language model, and no part of it calls one. Untrusted text (names
and spreadsheet cells) is only normalized and compared by RapidFuzz; it is never
interpreted as instructions. The only outbound calls go to Wikidata and GLEIF and
carry company names, so there is no path by which uploaded data could make the app
send private data elsewhere. AI coding assistants were used during development; the
code they produced is reviewed, tested and linted like any other.

**TODO(Norman): Discussed with the team on <date>.**

## Migrations

There is no database, so there are no schema migrations. The only files the app
writes are upload job files (`meta.json`, `cache.json`), which expire after an hour.
They are read leniently: unknown fields are ignored and missing ones get defaults,
so the version before and after a deploy can read each other's files during a
rollback (expand/contract without numbered migrations). The alias index is a
read-only file; a new one is checked by `backup_index.py` (checksum, count, that it
loads) before it replaces the old one.

## Not covered

- No WAF or bot protection beyond the rate limits.
- Rate limits and the job store are per process; see [scaling](scaling.md).
- TLS terminates at Caddy; traffic between Caddy and the api is plain HTTP on the
  private compose network.
- OS patching on the instance is left at the image's defaults; we did not check or
  change it.
