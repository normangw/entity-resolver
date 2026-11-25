# Time limits

Every call from one program to another has an explicit limit. This table lists each
one, where it is set, and why it has that value. Each limit sits above the slowest
normal request for that hop and below the limit of the hop in front of it, so a
slow dependency turns into a clear error (`503` with `Retry-After`, a `retry` row,
or a page message) instead of a hang.

`docs/architecture.md` shows where each one sits in the diagrams, where row 7 is
labelled **T7**, and so on.

## Measured baselines

From `scripts/loadtest.py` (README, "Load test"; in process, fake Wikidata at 50 ms
per call): `/resolve` from the local index p99 216 ms; `/resolve` through the
Wikidata path p99 529 ms; a 24-name batch 3.0 s. Live Wikidata is slower and
throttles: a lookup is 2 to 5 API calls, and each call took well under 2 s in the
2026-11-25 evaluation runs from a laptop.
<!-- TODO(#106): replace with p50/p99 measured on Jetstream (scripts/loadtest.py and monitor.py output). -->

## The table

| # | Call (from -> to) | Limit | Set in | Why this value |
|---|---|---|---|---|
| 1 | Browser -> Caddy: `POST /resolve` | 45 s | `static/index.html` `LIMIT_MS.resolve` | Above the server's worst case for one Wikidata lookup (row 9) with room for a second name variant. On timeout the page says "No answer in time". |
| 2 | Browser -> Caddy: `POST /upload` | 120 s | `LIMIT_MS.upload` | A 10 MB file over a slow (~1 Mbit/s) link; matches Caddy's 120 s. |
| 3 | Browser -> Caddy: preview, start a job | 30 s | `LIMIT_MS.preview`, `LIMIT_MS.start` | Reading a 5,000-row sheet takes well under a second; 30 s means something is wrong. |
| 4 | Browser -> Caddy: job status poll, `/ready` | 10 s, 5 s | `LIMIT_MS.status`, `LIMIT_MS.ready` | These answer from memory in milliseconds. |
| 5 | Client -> Caddy: request headers | 15 s | `Caddyfile` `timeouts read_header` | Drops clients that open a connection and send nothing (slowloris). |
| 6 | Caddy -> api: connect | 5 s | `Caddyfile` `dial_timeout` | The api is on the same Docker network; a connect takes milliseconds. 5 s means it is down or restarting, and the client gets a `502` instead of a hang. |
| 7 | Caddy -> api: response | 120 s | `Caddyfile` `response_header_timeout`, `read_timeout`, `write_timeout` | The longest normal request is a batch: budget 60 s (row 12) plus one lookup (row 9) ~ 85 s. 120 s leaves room and is the same as the old nginx value. |
| 8 | api: wait for a Wikidata slot | 5 s | `WIKIDATA_QUEUE_SECONDS` | At most `WIKIDATA_MAX_CONCURRENT` (4) lookups run at once. Waiting longer than 5 s means Wikidata is the bottleneck; the request gets `503` with `Retry-After: 5`. |
| 9 | api -> Wikidata: one whole lookup | 15 s deadline, ends within 15 s + one call | `WIKIDATA_DEADLINE_SECONDS` | No API call, retry or wait starts after 15 s. Worst case for one `/resolve` name: queue 5 + deadline 15 + one call 5 (production) = 25 s; 30 s in development, where one call may take 10 s. Well under rows 1 and 7. |
| 10 | api -> Wikidata: one API call | 5 s production, 10 s development | `WIKIDATA_TIMEOUT` (urllib `timeout=`) | A normal call takes well under 2 s. urllib has no default timeout (it would wait forever), so this is always set. |
| 11 | api -> Wikidata: retries and the 429 wait | 2 retries, backoff 0.25 s x 2^n with jitter; one wait of at most 2 s for a `429` | `WIKIDATA_RETRIES`, `WIKIDATA_MAX_RETRY_AFTER` | Timeouts, dropped connections and 5xx may pass on a second try. A `429` is waited out only when Wikidata asks for 2 s or less and the wait fits in the deadline; otherwise our caller gets `503` with Wikidata's `Retry-After`. A lookup counts once toward the breaker, however many calls it made. |
| 12 | api: one `/resolve/batch` | 60 s budget | `BATCH_BUDGET_SECONDS` | After 60 s the batch stops calling Wikidata and marks the rest `retry`, so the call ends inside Caddy's 120 s (row 7). |
| 13 | api: circuit breaker | open 30 s after 5 failed lookups in a row | `WIKIDATA_BREAKER_FAILURES`, `WIKIDATA_BREAKER_SECONDS` | While Wikidata is down, requests get `503` at once (with `Retry-After` = the time left) instead of each waiting for row 9. |
| 14 | api -> GLEIF | 5 s per request and for the whole tiebreak | `GLEIF_TIMEOUT` (urllib `timeout=` and `asyncio.wait_for`) | Off by default. A failed or slow tiebreak leaves the answer `unresolved`. |
| 15 | Upload job (background) | no HTTP limit; each name bounded by rows 8 to 11 | `uploads.process` | The job runs after `POST .../resolve` has answered `202`; the page shows progress. A name whose lookup fails is marked `retry`, and a re-run redoes only those rows. |
| 16 | api: shutdown | 20 s graceful, 30 s before Docker kills it | Dockerfile `--timeout-graceful-shutdown 20`, `compose.yml` `stop_grace_period: 30s` | Lets running requests finish and upload jobs save their state. |
| 17 | Docker -> api healthcheck | 2 s request, 3 s check, every 30 s | `Dockerfile` `HEALTHCHECK` | `/health` answers from memory. |
| 18 | Docker -> proxy healthcheck | 3 s, every 30 s | `compose.yml` | Goes through Caddy to `/health`. |
| 19 | `deploy.sh` -> the stack | 5 s per `curl`, 180 s to become healthy and ready | `scripts/deploy.sh` (`--max-time 5`, `DEPLOY_TIMEOUT`) | Loading the full index takes ~10-20 s; 180 s covers a slow first pull. |
| 20 | `monitor.py` -> the service | 30 s per request, 10 s for the webhook | `scripts/monitor.py` | Above row 9's worst case, so a slow but working Wikidata path is not reported as down. |
| 21 | `build_index.py` -> Wikidata SPARQL | 90 s per query | `scripts/build_index.py` | SPARQL queries over all listed companies are slow; this is an offline job. |

## Client defaults we do not rely on

- `urllib.request.urlopen` has no timeout by default: every call in `src/` and
  `scripts/` passes one.
- `fetch` in the browser has no timeout: every call in the page goes through
  `call()`, which aborts after the limit in `LIMIT_MS` (a test checks there is no
  other `fetch`).
- Caddy's `reverse_proxy` has no response timeout by default: row 7 sets one.
