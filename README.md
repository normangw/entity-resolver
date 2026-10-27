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
| `MATCH_THRESHOLD` | `0.8` | Minimum RapidFuzz score, as a fraction, to accept a match |
| `MODEL_DELAY_SECONDS` | `0` | Seconds `Resolver.resolve` sleeps before scoring. Stands in for a slow model; leave at `0` in normal use |
| `ALIAS_INDEX_PATH` | bundled `companies.json` | Local alias index loaded once at startup |

## Running the service

```bash
uv run uvicorn entity_resolver.main:app --reload
```

The service will be available at `http://localhost:8000`.

Interactive API docs: `http://localhost:8000/docs`

## Web UI

The service also serves a one-page web UI at the root URL, `http://localhost:8000/` (on Jetstream2, `http://<instance-public-ip>:8000/`). It is a single static file, `src/entity_resolver/static/index.html`, returned by `GET /` from the same FastAPI app and container, so there is no separate frontend to build or deploy, and it loads nothing from the internet.

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

`compose.yml` runs one service, `api`, on port 8000. It has no dependencies yet (no database, vector store or cache). The alias index is kept in the named volume `alias-index`, and the service uses `restart: unless-stopped`, so it comes back by itself after a reboot or a Jetstream2 unshelve.

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
4. Allow inbound TCP 8000 in the instance's security group in Exosphere/Horizon.
5. From your own laptop, check it answers from outside:

```bash
export HOST=<instance-public-ip>
curl http://$HOST:8000/health
```

6. Open `http://$HOST:8000/` in a browser to use the web UI.

### Call every endpoint

Set `HOST` to `localhost` on your machine, or to the instance's public IP.

```bash
# Health check
curl http://$HOST:8000/health
# {"status":"ok","version":"0.1.0"}

# Readiness check (503 {"status":"not ready"} until the alias index has loaded)
curl -i http://$HOST:8000/ready
# {"status":"ready"}

# Resolve a name in English
curl -X POST http://$HOST:8000/resolve \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Wal-Mart Stores Inc.", "language": "en"}'

# Resolve a non-English alias
curl -X POST http://$HOST:8000/resolve \
  -H "Content-Type: application/json" \
  -d '{"company_name": "上海航空", "language": "zh"}'

# A name not in the index comes back unresolved
curl -X POST http://$HOST:8000/resolve \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Zyxwv Quantum Bakery"}'

# Invalid input (blank name) is rejected with 422
curl -i -X POST http://$HOST:8000/resolve \
  -H "Content-Type: application/json" \
  -d '{"company_name": "   "}'
```

Interactive docs: `http://$HOST:8000/docs`

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

### `GET /health`
Check that the service is running.

```bash
curl http://localhost:8000/health
```

```json
{"status": "ok", "version": "0.1.0"}
```

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
  "headquarters": "Bentonville, Arkansas, United States",
  "country": "US",
  "confidence": 1.0
}
```

`region` is optional and defaults to `world`. It limits matching to companies headquartered in that region: `world`, `us`, `cn`, `gb`, `jp` or `eu`. A company outside the chosen region comes back `unresolved`. Each record in the index carries an ISO `country` code that the filter uses.

Names are normalized before scoring, the same way for the query and for every alias: Unicode NFKC, accent folding (`Nestlé` to `nestle`), lowercasing, `&` to `and`, leading "the", legal suffixes (`Inc`, `GmbH`, `AG`, `S.A.`, `有限公司`, `株式会社` and others) and punctuation removed. If that would leave nothing, the unstripped name is used.

A name that scores below `MATCH_THRESHOLD` returns `"status": "unresolved"` and null company fields. `language` is validated on the request. Matching uses the alias strings in the index, including non-English aliases such as `上海航空`.

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
