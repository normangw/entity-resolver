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
