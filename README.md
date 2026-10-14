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

## API Endpoints

### `GET /health`
Check that the service is running.

```bash
curl http://localhost:8000/health
```

```json
{"status": "ok", "version": "0.1.0"}
```

### `POST /resolve`
Resolve a company name against the local alias index. The model is loaded once at startup. The synchronous score runs in a worker thread (`asyncio.to_thread`) so a slow match does not block `/health`.

```bash
curl -X POST http://localhost:8000/resolve \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Wal-Mart Stores Inc.", "language": "en"}'
```

```json
{
  "input": "Wal-Mart Stores Inc.",
  "status": "resolved",
  "wikidata_id": "Q483551",
  "canonical_name": "Walmart",
  "parent_company": null,
  "headquarters": "Bentonville, Arkansas, United States",
  "confidence": 1.0
}
```

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
