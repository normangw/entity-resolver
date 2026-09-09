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
Resolve a company name to its canonical entity (placeholder — ML model coming in future assignments).

```bash
curl -X POST http://localhost:8000/resolve \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Wal-Mart Stores Inc.", "language": "en"}'
```

```json
{
  "input": "Wal-Mart Stores Inc.",
  "canonical_name": null,
  "parent_company": null,
  "headquarters": null,
  "confidence": null
}
```

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
