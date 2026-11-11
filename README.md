# Entity Resolver

A multilingual company entity resolution API. Given a messy or variant company name (e.g. "Wal-Mart Stores Inc." or "Shanghai Airlines"), the API returns the canonical company name, parent company, and headquarters address.

Built for DSAN 6700 — ML Deployment, Georgetown University.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Installation

```bash
git clone https://github.com/normangw/entity-resolver.git
cd entity-resolver
uv sync --extra dev --frozen
```

## Configuration

Copy the example environment file and edit as needed:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `APP_NAME` | `Entity Resolver API` | Name of the service |
| `APP_VERSION` | `0.1.0` | Version string |
| `DEBUG` | `false` | Enable debug mode |
| `WIKIDATA_TIMEOUT` | `10` | Timeout in seconds for Wikidata requests |
| `WIKIDATA_FALLBACK` | `true` | When a name is not in the local index, ask Wikidata (resolves subsidiaries such as YouTube or Beats Electronics). The test suite sets it to `false` (see `tests/conftest.py`) so CI stays offline. Set `false` to run fully offline |
| `WIKIDATA_CACHE_TTL` | `3600` | Seconds to remember a Wikidata answer, including "not found" (0 turns it off). Failures are never cached |
| `MATCH_THRESHOLD` | `0.9` | Minimum RapidFuzz score, as a fraction, to accept a match |
| `MODEL_DELAY_SECONDS` | `0` | Seconds `Resolver.resolve` sleeps before scoring. Stands in for a slow model; leave at `0` in normal use |
| `GLEIF_ENABLED` | `false` | Turn on the GLEIF tiebreak for low-confidence matches. When on, the service calls the public GLEIF API |
| `GLEIF_LOW_THRESHOLD` | `0.6` | Lowest score a candidate may have to be sent to the tiebreak |
| `GLEIF_DAILY_BUDGET` | `1000` | GLEIF requests allowed per day. GLEIF is free but shared, so this is a courtesy cap |
| `GLEIF_TIMEOUT` | `5` | Seconds to wait for GLEIF before giving up |
| `UPLOAD_DIR` | `<system temp>/entity-resolver-uploads` | Where uploaded spreadsheets and results are kept until they expire |
| `UPLOAD_MAX_BYTES` | `10485760` | Largest accepted upload (10 MB) |
| `UPLOAD_MAX_ROWS` | `5000` | Most data rows resolved per file |
| `UPLOAD_TTL_SECONDS` | `3600` | Uploads and results are deleted this long after upload |
| `UPLOAD_MAX_RUNNING` | `3` | Files resolved at the same time; more get `429` |
| `ALIAS_INDEX_PATH` | bundled `companies.json` | Local alias index loaded once at startup |
| `API_KEY` | unset | Shared secret sent as the `X-API-Key` header. Unset means open (the public demo). When set it is required on `/resolve/batch` and `/metrics` |
| `API_KEY_PROTECT_ALL` | `false` | Also require the key on `/resolve` and `/upload*`. The web page cannot send a key, so this turns the page off |
| `RATE_LIMIT_RESOLVE_PER_MIN` | `120` | `/resolve` requests per client address per minute (`0` = no limit); over it, `429` with `Retry-After` |
| `RATE_LIMIT_BATCH_PER_MIN` | `10` | Same for `/resolve/batch` |
| `RATE_LIMIT_UPLOAD_PER_MIN` | `20` | Same for `/upload` and `/upload/{id}/resolve` |
| `MAX_BODY_BYTES` | `262144` | Largest JSON body; over it, `413` (uploads have `UPLOAD_MAX_BYTES`) |
| `BATCH_MAX_NAMES` | `100` | Most names per `/resolve/batch` call |
| `BATCH_BUDGET_SECONDS` | `60` | After this long a batch stops calling Wikidata and marks the rest `retry` |
| `WIKIDATA_MAX_CONCURRENT` | `4` | Wikidata lookups in flight at once across all requests |
| `WIKIDATA_QUEUE_SECONDS` | `5` | How long a request waits for a Wikidata slot before a `503` |
| `WIKIDATA_RETRIES` | `2` | Extra attempts per Wikidata API call for timeouts and 5xx, with exponential backoff and jitter. HTTP 429 is never retried |
| `WIKIDATA_BREAKER_FAILURES` | `5` | Failed lookups in a row that open the circuit breaker |
| `WIKIDATA_BREAKER_SECONDS` | `30` | How long the breaker stays open (lookups get an immediate `503`) before one probe is let through |
| `LOG_FORMAT` | `json` | `json` (one object per line with a request id) or `text` |
| `LOG_LEVEL` | `INFO` | Log level |
| `BUILD_ID` | `unknown` | Reported as `build` by `/health`; set at image build time (see [Operating the service](#operating-the-service)) |

## Running the service

```bash
uv run uvicorn entity_resolver.main:app --reload
```

The service will be available at `http://localhost:8000`.

Interactive API docs: `http://localhost:8000/docs`

## Web UI

The service also serves a one-page web UI at the root URL, `http://localhost:8000/` (on Jetstream2 behind the proxy, `http://<instance-public-ip>/`). It is a single static file, `src/entity_resolver/static/index.html`, returned by `GET /` from the same FastAPI app and container, so there is no separate frontend to build or deploy, and it loads nothing from the internet.

How to use it:

1. Open the root URL in a browser. The badge in the top right reads **model ready** once `/ready` returns 200 (it shows **model loading** while the alias index loads).
2. Type a company name, optionally pick a language and region, and press **Resolve**. The page sends `POST /resolve` with `company_name`, `language` and `region`.
3. Or click one of the example chips. They cover name variants (`Wal-Mart Stores Inc.`, `Apple Computer`, `Google LLC`), non-English names (`上海航空`, `沃尔玛`, `阿里巴巴集团`), a region filter that excludes the match (`Apple Inc.` with region China) and a name that is not in the index (`Zyxwv Quantum Bakery`).

What it shows:

| Response | Display |
|---|---|
| `status: "resolved"` | Green card with the canonical name, parent company, headquarters, country, a link to the Wikidata entry and the match confidence |
| `status: "unresolved"` | Amber card saying no company scored above `MATCH_THRESHOLD` (and within the region, if one was chosen) |
| `503` | "Model still loading" message |
| `422` / blank input | "Invalid input" message with the validation error |

Each result has a **Raw JSON** toggle with the exact API response. `GET /` is left out of the OpenAPI schema, so `/docs` lists only the API endpoints.

### Resolve a spreadsheet

Below the single-name form, **Resolve a spreadsheet** takes an `.xlsx`, `.xls` or `.csv` file:

1. Choose the file. The page uploads it (`POST /upload`) and shows the first rows.
2. Pick the **sheet** (workbooks with several), tick or untick **First row is a header**, pick the **company name column** (the likely one is preselected) and the **region**.
3. Press **Resolve file**. A progress bar follows the job; each unique name is resolved once.
4. Read the summary and the integrity checks, then **Download** the result: your original rows and columns, unchanged, with nine columns appended (`Resolver status`, `Canonical name`, `Wikidata ID`, `Parent company`, `Headquarters`, `Country`, `Confidence`, `Source`, `Needs review`). `Needs review` is `yes` on rows to check, and the summary counts them. A job where Wikidata was busy ends **Partly done**, not Done: rows marked `retry` were never looked up (this is not a "no such company" answer), so resolve the file again later. Phone numbers and dashes keep their text; other cells that start with `=`, `+`, `-` or `@` get a leading apostrophe so Excel cannot run them as formulas. A csv comes back as csv; `.xlsx` and `.xls` come back as `.xlsx`.

Row statuses are `resolved`, `unresolved`, `blank` (empty name cell) and `skipped` (name over 200 characters). No row is ever dropped or reordered.

**File handling.** CSV files are read as UTF-8, UTF-16 (with a byte-order mark), GBK or Windows-1252, and the page says when a file was not UTF-8; a file that fits none is refused with a request to re-save it as UTF-8. Comma, semicolon, tab and pipe delimiters are detected from the first line, and the download is always comma-separated UTF-8. Header text is kept exactly as uploaded. If your file already has a column named like one of ours (`Resolver status`, `Country`), ours is renamed with a `(2)` suffix. For `.xlsx` and `.xls` only the chosen sheet comes back, as values only: other sheets, formulas, formatting, hidden columns and frozen panes are not kept. If Wikidata is busy, affected rows are marked `retry`, local matches are kept, and the job finishes; resolve the file again to fill the retry rows. Uploads are deleted after `UPLOAD_TTL_SECONDS` and stop being downloadable at that moment.

**Integrity checks.** After writing the result the service reads it back and compares it with the input: row count, original headers, every original cell, every company name, a valid status on every row, and a name, Wikidata ID and confidence on every resolved row. If any check fails the page says so and the file should not be used.

**Safety.** The file type is read from the file's bytes and must match the extension. Macro workbooks (`.xlsm`, or a `.xlsx` holding macros) and zip bombs are refused. Formulas are never evaluated, and text that could run as a formula in Excel is written back as plain text. Uploads are deleted `UPLOAD_TTL_SECONDS` after upload, and cell contents are never logged.

## Running with Docker

The service ships as one container image. Configuration is read at run time from `.env`; nothing secret is baked into the image (see `.dockerignore`). Where the model lives, and why, is written at the top of the `Dockerfile`.

Requirements: Docker Engine with the Compose plugin (`docker compose version`).

### Start the stack

On a fresh clone, this is the whole start procedure:

```bash
git clone https://github.com/normangw/entity-resolver.git
cd entity-resolver
cp .env.example .env        # optional: without .env the built-in defaults apply
docker compose up -d        # builds the image on first run, then starts it
docker compose ps           # STATUS should show "(healthy)" after ~10 s
```

`compose.yml` runs two services: `api` (port 8000, not published) and `proxy`, an nginx reverse proxy that publishes port 80 and forwards to `api`. `nginx.conf` limits each client IP to 10 requests per second with a burst of 30 (extra requests get `429`), caps request bodies at 12 MB, hides the nginx version, and adds `X-Content-Type-Options`, `Referrer-Policy` and `X-Frame-Options` headers. The API sees the real client address through `X-Forwarded-For`. There is no database, vector store or cache. The alias index is kept in the named volume `alias-index`, and the service uses `restart: unless-stopped`, so it comes back by itself after a reboot or a Jetstream2 unshelve.

Useful commands:

```bash
docker compose logs -f api              # follow the logs
docker compose up -d --build            # rebuild after a code change
docker compose down                     # stop (keeps the volume)
docker compose down -v                  # stop and delete the volume (reseeds the index next start)
```

To build and run the image without Compose:

```bash
docker build -t entity-resolver:latest .
docker run -d --name entity-resolver -p 8000:8000 --env-file .env entity-resolver:latest
```

### Deploy on Jetstream2

1. SSH to the instance and install Docker if it isn't there (`curl -fsSL https://get.docker.com | sudo sh`, then `sudo usermod -aG docker $USER` and log in again).
2. Make sure Docker starts at boot: `sudo systemctl enable docker`.
3. Clone and start the stack with the commands above.
4. Allow inbound TCP 80 in the instance's security group in Exosphere/Horizon, and remove the old rule for 8000.
5. From your own laptop, check it answers from outside:

```bash
export HOST=<instance-public-ip>
curl http://$HOST/health
```

6. Open `http://$HOST/` in a browser to use the web UI.

### Call every endpoint

Set `HOST` to `localhost` on your machine, or to the instance's public IP.

```bash
# Health check
curl http://$HOST/health
# {"status":"ok","version":"0.1.0"}

# Readiness check (503 {"status":"not ready"} until the alias index has loaded)
curl -i http://$HOST/ready
# {"status":"ready"}

# Resolve a name in English
curl -X POST http://$HOST/resolve \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Wal-Mart Stores Inc.", "language": "en"}'

# Resolve a non-English alias
curl -X POST http://$HOST/resolve \
  -H "Content-Type: application/json" \
  -d '{"company_name": "上海航空", "language": "zh"}'

# A name not in the index comes back unresolved
curl -X POST http://$HOST/resolve \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Zyxwv Quantum Bakery"}'

# Invalid input (blank name) is rejected with 422
curl -i -X POST http://$HOST/resolve \
  -H "Content-Type: application/json" \
  -d '{"company_name": "   "}'
```

Interactive docs: `http://$HOST/docs`

### Container measurements

Measured on the team's Jetstream2 instance with these commands:

```bash
docker image ls entity-resolver                                      # image size on disk
echo "# touch" >> src/entity_resolver/main.py
time docker compose build                                            # rebuild, dependency layer cached
time docker compose build --no-cache                                 # rebuild, nothing cached
git checkout src/entity_resolver/main.py
docker system df                                                     # with the stack up
```

| Measurement | Result |
|---|---|
| Image size on disk | 322 MB (79.9 MB content size) |
| Rebuild after a one-line code change, dependency layer cached | 3.5 s |
| Rebuild after a one-line code change, `--no-cache` | 9.3 s |

`docker system df` with the stack up:

```
TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE
Images          5         5         2.257GB   2.257GB (100%)
Containers      5         4         27.18MB   12.29kB (0%)
Local Volumes   2         2         270.8MB   0B (0%)
Build Cache     40        0         691.5MB   369.1MB
```

With the dependency layer cached, only `COPY src` and the final `uv sync` re-run after a one-line code change; the base image, `uv`, and the dependency install steps are all `CACHED`. `--no-cache` re-runs every step, but still reuses the already-pulled base images. The stack came back by itself after shelving and unshelving the instance (`restart: unless-stopped`), and `/health` answered again.

## API Endpoints

### `GET /`
Serve the web UI (HTML). See [Web UI](#web-ui). Not listed in `/docs`.

### Spreadsheet endpoints

| Endpoint | Purpose |
|---|---|
| `POST /upload` (multipart `file`) | Save and inspect a file. Returns `id`, `kind`, `sheets`, `max_rows`. `400` for a bad or refused file, `413` if too large |
| `GET /upload/{id}/preview?sheet=&has_header=` | Column labels, first rows and a suggested name column |
| `POST /upload/{id}/resolve` | Body `{"sheet", "has_header", "column", "region"}`. Starts the job (`202`). `400` for a bad choice, `409` if already running, `429` if too busy, `503` while the model loads |
| `GET /upload/{id}/status` | `state` (`uploaded`, `running`, `done`, `error`), progress, and the summary with the integrity checks when done |
| `GET /upload/{id}/download` | The resolved file (`409` until done) |

```bash
curl -F file=@companies.xlsx http://localhost:8000/upload
# {"id":"<id>","filename":"companies.xlsx","kind":"xlsx","sheets":["Sheet1"],"max_rows":5000}
curl -X POST http://localhost:8000/upload/<id>/resolve \
  -H 'Content-Type: application/json' -d '{"column": 0, "region": "us"}'
curl http://localhost:8000/upload/<id>/status
curl -OJ http://localhost:8000/upload/<id>/download
```

### `POST /resolve/batch`

Resolve up to `BATCH_MAX_NAMES` (100) names in one call. Results come back in input order, each distinct name is resolved once, and a name whose Wikidata lookup is unavailable comes back with `status: "retry"` instead of failing the whole call. Send those names again later. After `BATCH_BUDGET_SECONDS` the call stops calling Wikidata and returns `retry` for names the local index cannot settle, so it ends inside the proxy's 120 s timeout. Needs the `X-API-Key` header when `API_KEY` is set.

```bash
curl -X POST http://localhost:8000/resolve/batch \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"company_names": ["Wal-Mart Stores Inc.", "YouTube", "Zyxwv Quantum Bakery"], "region": "world"}'
```

```json
{"results": [{"input": "Wal-Mart Stores Inc.", "status": "resolved", "...": "..."}], "unique_names": 3, "retry": 0}
```

`422` for an empty list, more than `BATCH_MAX_NAMES` names, or a blank or over-200-character name.

### `GET /metrics`
Counters and request latency in Prometheus text format (not listed in `/docs`; `404` through the nginx proxy, so read it from inside: `docker compose exec api python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/metrics').read().decode())"`). Needs the API key when `API_KEY` is set. Series: `http_requests_total{method,path,status}` (route templates, never user text), `http_request_duration_seconds` (histogram), `resolutions_total{source,status}` (`source` is `local`, `wikidata`, `gleif` or `none`; a 503 is counted with its status code), `wikidata_unavailable_total`, `wikidata_busy_total` (requests that waited too long for a Wikidata slot), `rate_limited_total{scope}`, `auth_failures_total` and `process_uptime_seconds`. Counters reset when the process restarts.

### `GET /health`
Check that the service is running.

```bash
curl http://localhost:8000/health
```

```json
{"status": "ok", "version": "0.1.0", "build": "a1b2c3d"}
```

`version` comes from `pyproject.toml` and does not change between deployments. `build` is the `BUILD_ID` the image was built with (a commit hash, see [Operating the service](#operating-the-service)), so it tells you what is actually running. It is `unknown` if the image was built without one.

### `GET /ready`
Check that the alias index has finished loading. The model loads in the background after startup, so `/health` answers as soon as the process is up, while `/ready` and `/resolve` return `503` until loading completes.

```bash
curl -i http://localhost:8000/ready
```

```json
{"status": "ready"}
```

Before the load finishes (or if it fails) the response is `503` with `{"status": "not ready"}`.

### `POST /resolve`
Resolve a company name against the local alias index. The model is loaded once at startup, in the background; until it is ready this returns `503`. The synchronous score runs in a worker thread (`asyncio.to_thread`) so a slow match does not block `/health`.

```bash
curl -X POST http://localhost:8000/resolve \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Wal-Mart Stores Inc.", "language": "en", "region": "world"}'
```

```json
{
  "input": "Wal-Mart Stores Inc.",
  "status": "resolved",
  "wikidata_id": "Q483551",
  "canonical_name": "Walmart",
  "parent_company": null,
  "headquarters": "Bentonville, United States",
  "country": "US",
  "confidence": 1.0,
  "source": "local",
  "needs_review": false,
  "matched_alias": "Wal-Mart Stores, Inc.",
  "as_of": "2026-10-25",
  "note": null
}
```

This example matches the deployed app: the Jetstream instance runs `main` (earlier README drafts showed a placeholder response with null fields; that was the stage-one stub). `GET /openapi.json` or `/docs` on the running app is always the authoritative schema. `source` is `"local"` for a local match, `"wikidata"` for a fallback match, and null when unresolved.

With `WIKIDATA_FALLBACK=true`, a name that misses the index is looked up on Wikidata, and `parent_company` is read from its `parent organization` statement (`Beats Electronics` resolves to `Apple Inc.`) or, failing that, from an `owned by` statement (`YouTube` to `Google`). `owned by` also records shareholders, so it only counts when it names exactly one owner that holds a majority or an unstated stake and is not a person. A widely held company such as Caterpillar, or a founder-controlled one such as Foxconn, resolves with `parent_company: null`. A parent named like the company itself (`WhatsApp LLC` for WhatsApp) is a legal-entity wrapper and is skipped in favour of its owner. Wikidata hits carry no country, so the fallback is skipped when `region` is not `world`. If Wikidata cannot answer (timeout, or HTTP 429 when it throttles), `/resolve` returns `503` rather than `unresolved`, so an outage is never reported as "no such company". A company Wikidata has no owner for (such as Instagram) resolves with `parent_company: null`.

`canonical_name` is always the English name, so one company is one name for joins. With `language` set to another supported code (`zh`, `ja`, `de`, `fr`, `es`), a company found on Wikidata also returns `label` (its name in that language), and its parent and headquarters are in that language too. Local-index matches have no `label`. Other language codes are rejected with 422. The spreadsheet flow has no language option and always returns English.

`region` is optional and defaults to `world`. It limits matching to companies headquartered in that region: `world`, `us`, `cn`, `gb`, `jp` or `eu`. A company outside the chosen region comes back `unresolved`. Each record in the index carries an ISO `country` code that the filter uses.

Names are normalized before scoring, the same way for the query and for every alias: Unicode NFKC, accent folding (`Nestlé` to `nestle`), lowercasing, `&` to `and`, leading "the", legal suffixes (`Inc`, `GmbH`, `AG`, `S.A.`, `有限公司`, `株式会社` and others) and punctuation removed. If that would leave nothing, the unstripped name is used.

`confidence` is name similarity (0 to 1) between the normalized input and the matched name. It is **not** a probability that the match is right: 1.0 means the text matched a known name, not that it is the company you mean, and 0.95 is not "95% likely correct". Local matches use RapidFuzz `ratio`; Wikidata matches use `WRatio`, so scores are comparable within one `source` but not across sources. The page labels it "Name similarity". There is no single cutoff that suits every workflow: treat anything with `needs_review: true`, or any score below 1.0, as a row to check. Sorting by `confidence` is safe within a source; to compare across sources, filter on `source` first. `matched_alias` shows the stored name the input matched, and `as_of` is the date the index was built (local) or the lookup was made (Wikidata), so an answer can be explained later.

Input is checked before matching. Text with fewer than two letters or digits (`T`, `?`, an emoji) and placeholders such as `TBD`, `N/A` or `unknown` are never resolved, and index aliases that normalize to nothing are dropped. If the name as typed does not resolve, the service retries it without brackets and vendor numbers (`Microsoft Corporation (Vendor #4471)`), then without a trailing city and state (`Walmart, Bentonville AR`). For `X d/b/a Y` it tries the text, then X (the legal entity), then Y (the brand), and the first that resolves wins. When several index companies tie on the same name, a top-level company beats a subsidiary, then the lowest Wikidata Q-number wins (older, more notable entities have lower numbers) and `needs_review` is set. `note` explains why a result needs a look when the name is known to have been split or merged. `needs_review` is `true` when two companies tied on score or the score is under 0.95, so an analyst should check that row. Names shorter than 8 characters (after normalization) must match exactly, and bare words such as `Bank`, `Inc.` or `USA` are never resolved, because one changed letter or a generic word otherwise lands on an unrelated company (`Lexus` on `Plexus`). When several Wikidata companies share a name, the one with the most Wikipedia sitelinks wins and `needs_review` is set. Wikidata is always searched in English (25 hits) so the same name finds the same company in every language; `language` only chooses the labels returned. Hits whose `instance of` is a building, university, international organization, government agency, nonprofit or similar class are skipped; the list is not exhaustive, so a non-company can still slip through.

`source` says where the answer came from: `local` (the alias index) or `gleif` (settled by the GLEIF tiebreak).

**GLEIF tiebreak.** When `GLEIF_ENABLED=true` and the best local score is below `MATCH_THRESHOLD`, candidates scoring at least `GLEIF_LOW_THRESHOLD` that have an `identifier` (the company's Legal Entity Identifier, LEI) are sent to the public GLEIF API in one request (`GET /lei-records?filter[lei]=...`). GLEIF's name search cannot handle messy input, but lookup by LEI is exact, so it confirms candidates the index proposed instead of finding new ones. The candidate whose GLEIF legal name best matches the query wins if it scores at least `MATCH_THRESHOLD`. Name, country and headquarters in the response still come from Wikidata, because GLEIF's address is often a registered agent's. A timeout, an error or a spent daily budget leaves the result `unresolved`. Only companies that have an LEI on Wikidata (property P1278) can be verified; `scripts/build_index.py` stores it as `identifier` and prints the count as `with_lei`.

A name that scores below `MATCH_THRESHOLD` (and is not settled by the tiebreak) returns `"status": "unresolved"` and null company fields. `language` is validated on the request. Matching uses the alias strings in the index, including non-English aliases such as `上海航空`.

## Design decisions and known limits

Answers to questions raised during user testing (issues #42 to #92).

- **Split and renamed companies.** `General Electric` resolves to GE Aerospace (the renamed parent; GE Vernova and GE HealthCare were spun off in 2023-24) and `Hewlett-Packard` to the old HP. The index cannot tell which successor an old contract belongs to, so names on a small list (`General Electric`, `Hewlett-Packard`, `Time Warner`) come back with `needs_review: true` and a `note` naming the successors. `Time Warner` still resolves to Time Warner Cable because that is the alias the index holds; the note says the media company is a different entity. Pick the successor by hand.
- **Brands and d/b/a names.** With the Wikidata fallback on by default, a brand resolves to its own entity and `parent_company` is its owner when Wikidata records one (`YouTube` to `Google`). A brand with no owner on record (`Sam's Club`, `Instagram` in some cases) has `parent_company: null`. In `X d/b/a Y` the legal entity X is tried first, then the brand Y.
- **Which alias matched.** `matched_alias` returns the stored alias. It does not yet say whether it was a ticker, an old name or a translation.
- **As-of date.** `as_of` is the index build date for local matches and the lookup date for Wikidata, whose parent and headquarters values can be out of date (`WarnerMedia`'s parent, `Dow`'s parent). Wikidata answers are cached for `WIKIDATA_CACHE_TTL` seconds.
- **Using the API directly.** It is meant to be used, one name per `POST /resolve`. For lists use the spreadsheet upload. There is no batch endpoint, API key or rate limit, so do not point a script at the public demo: it shares the Wikidata and GLEIF budget, and Wikidata throttles (you get `503`, not a wrong answer). Add a gateway rate limit before exposing it more widely.
- **Headquarters format.** `headquarters` is whatever Wikidata or the index stores, so it can be a city, district or building and may not match `country` (`Philip Morris`, `Tencent`, `HSBC`). It is not cleaned up. Treat it as a hint and `country` (local matches only) as the more reliable field.

- **Join key.** Join on `wikidata_id`. It is stable across runs and languages. `canonical_name` is always the English name but can change if Wikidata or the index is rebuilt. The index does not carry a version yet, so keep the file you resolved alongside its results. The stored `identifier` is the company's LEI (used only for the GLEIF tiebreak) and is not returned.
- **Entity level.** A match is the legal brand most people mean, not always the operating company: `Google LLC` resolves to `Alphabet Inc.` because the index treats the group as the canonical company. `parent_company` is one level up only, and it is `null` both when a company has no parent and when Wikidata has no owner on record, so a blank does not prove independence. Roll-ups across a corporate group need a second pass on `parent_company` and are not built in.
- **Country.** Local matches carry an ISO country. Wikidata fallback matches do not, so `country` is `null` for them (for example `上海航空`); the page says so.
- **Corrections.** There is no way to save a correction. Fix a wrong row in your own copy.
- **History.** Only current names and common aliases are indexed. A former name resolves only if it is an alias or still on Wikidata, and the response does not say whether a company is active or defunct.
- **Confidential data.** The service is a public demo. Unmatched names are sent to Wikidata (and GLEIF when enabled), and uploads sit on the server for `UPLOAD_TTL_SECONDS`. Do not upload confidential vendor lists.
- **Size.** Spreadsheets are limited to `UPLOAD_MAX_ROWS` data rows (5,000 by default) and there is no resume: reloading the page loses the upload, so the page asks for confirmation if you try to leave mid-job. Split larger lists.
- **Evaluation.** `eval/README.md` scores the local index only, on queries that were also used to pick the 0.90 threshold. The live app also answers from Wikidata, so its accuracy on unlisted names is different, and `Instagram` is an example that resolves through Wikidata. Treat the published numbers as a ceiling for the local index, not a guarantee for the whole app. `docs/deliverable2/architecture.html` is the Deliverable 2 snapshot and still describes `WRatio` at 0.8 (95.8%); the current decision is `ratio` at 0.90.
- **Spreadsheet versus single name.** Both use the same resolver and return the same fields; the spreadsheet adds a row status. Since `canonical_name` is always English, `上海航空` is `Shanghai Airlines` in both (the single-name card also shows the Chinese name as `label`).

## Operating the service

**Protection (all optional, set in `.env`).** The demo runs open. To lock it down: set `API_KEY` (batch and metrics need `X-API-Key`; add `API_KEY_PROTECT_ALL=true` for a script-only deployment), and tune the `RATE_LIMIT_*` values. Limits are per client address, in memory, per process, which fits the single container. Behind the nginx proxy the client address is the one nginx saw (it overwrites `X-Forwarded-For`; see below). JSON bodies over `MAX_BODY_BYTES` get `413` before they are read in full. At most `WIKIDATA_MAX_CONCURRENT` Wikidata lookups run at once; others wait up to `WIKIDATA_QUEUE_SECONDS` and then get `503`, so one script cannot exhaust Wikimedia's patience for everyone. The GLEIF daily budget is unchanged.

**Resilience.** Timeouts and 5xx from Wikidata are retried with backoff and jitter. HTTP 429 and bad replies are not retried (that would worsen throttling) and, as before, return `503` rather than `unresolved`. After `WIKIDATA_BREAKER_FAILURES` failed lookups in a row the circuit breaker opens: for `WIKIDATA_BREAKER_SECONDS` lookups get an immediate `503` and Wikidata is left alone, then one probe is let through. Local-index matches are never affected. On shutdown the app stops running upload jobs and saves where each stopped.

**Uploads survive a restart.** Each job writes `meta.json` and `cache.json` next to its files in `UPLOAD_DIR` (a named volume in `compose.yml`). After a restart, finished jobs can still be downloaded until `UPLOAD_TTL_SECONDS`, and a job that was running comes back as an error saying to resolve again. Resolving the same upload again reuses every answer except `retry` rows, so a second run only repeats the lookups Wikidata could not answer. `cache.json` holds the company names from the file, so it has the same lifetime and the same confidentiality as the upload itself.

**Logs.** `LOG_FORMAT=json` writes one object per line: time, level, logger, message and `request_id`. Each request also gets one access line with the method, the route template (not the URL or query), the status and the duration. The id is returned in the `X-Request-ID` response header, and a valid incoming `X-Request-ID` is kept, so a request can be followed through nginx and the app. Company names, spreadsheet cells and file names are never logged; there is a test for it.

**Deploying and verifying a build.**

```bash
BUILD_ID=$(git rev-parse --short HEAD) docker compose up -d --build
curl http://$HOST/health        # "build" must equal the commit you meant to deploy
docker compose ps               # api and proxy should both be "healthy"
```

**Container and proxy hardening.** The image is non-root (uid 1000), pinned to `python:3.12-slim-bookworm` and the uv `0.9.5` image, has a stdlib health check, and starts uvicorn with `--timeout-graceful-shutdown 20` (compose waits `stop_grace_period: 30s`). Compose sets memory, CPU and pid limits, `no-new-privileges`, drops all capabilities for the API, rotates container logs and adds a proxy health check. `nginx.conf` limits request rate and connections per IP, caps JSON bodies at 512 KB and uploads at 12 MB, drops slow clients, streams uploads without buffering, returns `404` for `/metrics`, and overwrites `X-Forwarded-For` with the real client address (appending to a client-supplied value would let anyone fake the address the app rate-limits on). **None of the compose or nginx changes has been run on a live host** (no Docker or nginx was available where they were written), so after the first start check `docker compose ps`, `docker compose exec proxy nginx -t`, an upload through port 80, and `docker compose logs api` for permission errors on `/data/uploads`. Base images are pinned by tag, not digest; pinning digests needs registry access.

**Security review.** Checked and found sound: uploaded file names are used only for display and the download name (reduced to `[\w.\- ]` so they cannot reach the `Content-Disposition` header); job ids are random 128-bit hex checked against a pattern before touching the disk, and a job directory read back after a restart cannot point outside itself; uploads stream to disk under a size limit, with zip-bomb limits on `.xlsx` and read-only workbook loading; CSV and Excel cells that could run as formulas are neutralized; outbound requests go only to the fixed Wikidata and GLEIF hosts, with user text only as URL-encoded query values (no SSRF surface); no user text reaches logs, metric labels (route templates and a fixed set of methods only) or response headers. Known gaps: `openpyxl` parses `.xlsx` XML without `defusedxml` (not installed, and adding it needs a lock update), so a crafted file could use XML entity expansion up to the existing 200 MB uncompressed limit; the app has no TLS (terminate it in front, since the proxy listens on port 80); rate limits and the job store are per process, so running more than one API replica would multiply them.

**Load test.** `scripts/loadtest.py` runs the real app in process with a fake Wikidata (it never touches the network):

```bash
PYTHONPATH=src uv run python scripts/loadtest.py --index data/companies.full.json
```

Measured on the author's Mac (one uvicorn-equivalent process, 32 client threads, full 14,127-company index, fake Wikidata at 50 ms per API call, 4 Wikidata slots; the TestClient adds overhead, so treat these as a floor for the service and not as network numbers):

| Scenario | Throughput | p50 | p95 | p99 | Status codes |
|---|---|---|---|---|---|
| `/resolve`, names in the index (400) | 207 req/s | 149 ms | 190 ms | 216 ms | 400 x 200 |
| `/resolve`, misses sent to Wikidata (200) | 66 req/s | 478 ms | 494 ms | 529 ms | 200 x 200 |
| `/resolve/batch`, 24 names, 20 misses (20 calls, 8 threads) | 2.8 calls/s | 2.8 s | 3.0 s | 3.0 s | 20 x 200 |

Local scoring is CPU-bound in worker threads, so it tops out near the core count of the machine. A miss costs one Wikidata API call at minimum (about 3 when a candidate is found) and is bounded by the slot cap: 4 slots at 50 ms per call allow about 80 misses per second at best, and real Wikidata latency is higher. In a separate overload run (bundled seed index, one slot, 200 ms per call, 80 + 40 requests from 32 threads) 15 requests waited the full 5 s for a slot and were shed as `503`, all counted by `wikidata_busy_total`; the rest were answered. Real Wikidata throttling will set the practical limit for names outside the index.

## Concurrency audit

`discussion.md` records one request's steps and the ten-request measurement. To reproduce it, run the blocking tag and this branch separately. In one terminal:

```bash
git checkout blocking-resolve
MODEL_DELAY_SECONDS=1 uv run uvicorn entity_resolver.main:app --host 127.0.0.1 --port 8000
```

In another:

```bash
uv run python scripts/bench.py http://127.0.0.1:8000 10
```

Stop the server, then repeat on this branch (`git checkout 5-fix-blocking-call`). `MODEL_DELAY_SECONDS` must be set before the process starts. The bench script sends ten `POST /resolve` calls at once with `httpx.AsyncClient` and `asyncio.gather` and prints the wall-clock time and the slowest request.

## Running tests

```bash
uv run pytest -v
```

## Code quality

```bash
uv run ruff check .       # lint
uv run ruff format .      # format
uv run mypy src/          # type check
```
