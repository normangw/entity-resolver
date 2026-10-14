# Blocking-call audit

`POST /resolve` is the proposal's baseline: normalize a company name, score it with RapidFuzz against the local alias index, and return the canonical name, Wikidata id, parent, and headquarters. A score under `MATCH_THRESHOLD` comes back `unresolved` instead of a forced match.

The handler is an `async def`. `Resolver.resolve` is synchronous. On this index the fuzzy score itself takes milliseconds, which is too fast to show whether the event loop is blocked, so `MODEL_DELAY_SECONDS=1` makes that call `time.sleep` for one second before scoring. The sleep stands in for a slow model. That is the same stand-in as the async lab, and the fix is the one the case study uses for a blocking retrieval: `await asyncio.to_thread(...)`.

One request, with `MODEL_DELAY_SECONDS=1`:

| Step | Where it runs | Computing or waiting | On what | About how long |
|---|---|---|---|---|
| Validate the JSON body | event loop | computing | Pydantic `ResolveRequest` | milliseconds |
| `Resolver.resolve` delay | the event loop in the tagged blocking version; a worker thread after the fix | waiting | `time.sleep`, standing in for the model | 1 s |
| RapidFuzz `WRatio` over the alias index | that same thread | computing | local `companies.json` | milliseconds |
| Build `ResolveResponse` | event loop | computing | the response model | milliseconds |

Ten concurrent `POST /resolve` requests, sent together with `httpx.AsyncClient` and `asyncio.gather` (`uv run python scripts/bench.py`), `MODEL_DELAY_SECONDS=1`. This machine has `cpu_count` 12, so the default pool is `min(32, 12 + 4) = 16` threads.

| Version | Git ref | 10 requests took | Slowest request |
|---|---|---|---|
| Blocking `async def` calls `resolver.resolve` directly | tag `blocking-resolve` | 10.1 s | 10.1 s |
| `await asyncio.to_thread(resolver.resolve, name)` | this branch | 1.0 s | 1.0 s |

The blocking version called `time.sleep` inside `async def`, so the event loop ran the ten one-second sleeps one after another and both the wall clock and the slowest request were 10.1 s. `asyncio.to_thread` runs that sleep on the worker pool; sixteen threads can hold all ten sleeps at once, so both numbers fell to 1.0 s.


# Where the model lives

**Decision:** the model runs inside the API container, as a library, on the same CPU as the web server. There is no separate model server and no hosted API.

The "model" is a RapidFuzz scorer (`fuzz.WRatio`) over a small alias index (`companies.json`). It is loaded once at startup (`lifespan` in `main.py`) and every request reuses it. It needs no GPU and no weights download.

## Options considered

| Option | Where the model lives | Cost |
|---|---|---|
| **A. In the API container (chosen)** | A library in the same process | No extra service and no network hop. The scorer shares the container's CPU and memory. |
| B. Separate model container | Its own container, reached by URL | A second service to build, health-check and restart, plus a network hop on every request. |
| C. Hosted API or GPU machine | Another machine, as in the course's vLLM setup | Per-call latency and cost, an outage dependency, and GPU upkeep. |

## Measurements (team Jetstream2 instance)

| Measurement | Result |
|---|---|
| Image size on disk | 322 MB (79.9 MB content size) |
| Rebuild after a one-line change, dependency layer cached | 3.5 s |
| Rebuild after a one-line change, `--no-cache` | 9.3 s |
| Time to answer one `POST /resolve` (median of 20, `curl` on the instance) | 1.3 ms |
| Container memory with the index loaded (`docker stats`) | 39.75 MiB |

The latency is end-to-end endpoint latency (request parsing, scoring and response), not scorer-only runtime. It was measured with `curl` from the instance itself, so it excludes the network between a client and the instance. The `MODEL_DELAY_SECONDS` stand-in from the blocking-call audit above was off (`0`).

## Why this placement

Scoring a name takes about 1.3 ms, so a network hop to a separate model service (option B) or a hosted API (option C) would cost more time than the model itself, and would add a second thing to keep running. The image is only 322 MB, and the container uses under 40 MiB of memory, because there are no model weights in it.

## What it costs

- The model shares the API's CPU. A heavier scorer would compete with request handling, so `Resolver.resolve` runs in a worker thread (`asyncio.to_thread`) to keep `/health` responsive; see the blocking-call audit above.
- The alias index is baked into the image. Changing `companies.json` needs `docker compose down -v` and a rebuild: `compose.yml` sets `ALIAS_INDEX_PATH` under `environment`, which overrides `.env`, and the `alias-index` volume keeps the file from the first start. Scaling the API means scaling the model with it.

## Moving the model later

Nothing in the endpoint code depends on where the model runs, and `ALIAS_INDEX_PATH` already comes from configuration. If the scorer is replaced by a heavier model, the resolver can become a client whose address is read from settings, but that client and its URL settings do not exist yet. They would have to be implemented first, and only then would moving the model be a configuration change.
