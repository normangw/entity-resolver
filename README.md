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

### Call every endpoint

Set `HOST` to `localhost` on your machine, or to the instance's public IP.

```bash
# Health check
curl http://$HOST:8000/health
# {"status":"ok","version":"0.1.0"}

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
docker image inspect entity-resolver:latest --format '{{.Size}}'      # image size (bytes)
echo "# touch" >> src/entity_resolver/main.py
time docker compose build                                            # rebuild, dependency layer cached
time docker compose build --no-cache                                 # rebuild, nothing cached
git checkout src/entity_resolver/main.py
docker system df                                                     # with the stack up
```

| Measurement | Result |
|---|---|
| Image size on disk | _fill in_ |
| Rebuild after a one-line code change, dependency layer cached | _fill in_ |
| Rebuild after a one-line code change, `--no-cache` | _fill in_ |

`docker system df` with the stack up:

```
(paste output here)
```

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
