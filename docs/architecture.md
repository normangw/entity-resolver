# Architecture

This document supersedes the Deliverable 2 snapshot in `docs/deliverable2/`
(which still shows nginx, port 8000 and `WRatio` at 0.8). The diagrams are Mermaid;
GitHub renders them in place. Numbers in square brackets, such as **[T7]**, are rows
of the [time-limits table](time-limits.md).

Parts marked *(planned, #102)* are Prometheus and Grafana, which the observability
work adds; they are drawn so the picture is complete, and are not in `compose.yml`
yet.

<!-- TODO(#106): export each diagram to SVG/PNG (mermaid-cli: npx @mermaid-js/mermaid-cli -i docs/architecture.md) if the slides need images. -->

## 1. Process view: one name, start to finish

What happens for `POST /resolve`. The spreadsheet flow runs the same resolver for
each unique name in a background task.

```mermaid
flowchart TD
    A[Client sends a name] --> B{Caddy: body under 512 KB? not /metrics?}
    B -- no --> B1[413 JSON or 404]
    B -- yes --> C{api: model loaded?}
    C -- no --> C1[503 + Retry-After: 5]
    C -- yes --> D{API key needed and valid? rate limit OK?}
    D -- no --> D1[401 / 429 + Retry-After]
    D -- yes --> E[Normalize: NFKC, accents, case, legal suffixes]
    E --> F{Placeholder, too short or generic?}
    F -- yes --> U[unresolved]
    F -- no --> G[RapidFuzz over the local alias index, region filter]
    G --> H{score at least MATCH_THRESHOLD?}
    H -- yes --> R[resolved, source local, needs_review if tie or under 0.95]
    H -- no --> I{GLEIF on and candidates at least GLEIF_LOW_THRESHOLD?}
    I -- yes --> J[GLEIF LEI lookup, T14] --> J1{confirmed?}
    J1 -- yes --> R2[resolved, source gleif]
    J1 -- no --> K
    I -- no --> K{Wikidata fallback on and region world?}
    K -- no --> V[Retry variants: brackets, city, d/b/a] --> U
    K -- yes --> L[Wait for a Wikidata slot, T8]
    L -- waited too long --> L1[503 + Retry-After]
    L --> M[Wikidata lookup under a deadline, T9-T11]
    M -- found --> R3[resolved, source wikidata, parent and HQ from P749/P127]
    M -- not a company / no hit --> V
    M -- 429, timeout, breaker open --> L1
```

## 2. Execution view: what runs where

```mermaid
flowchart LR
    subgraph Internet
        U1[Browser]
        U2[curl / scripts]
        LE[Let's Encrypt]
    end
    subgraph Instance["Jetstream2 instance (Docker, one host)"]
        direction LR
        subgraph net["compose network"]
            P["proxy: Caddy 2.11.8<br/>ports 80, 443 published<br/>TLS, headers, body limits"]
            API["api: uvicorn + FastAPI<br/>port 8000, not published<br/>one process, worker threads"]
            PR["prometheus (planned, #102)<br/>not published"]
            GR["grafana (planned, #102)<br/>127.0.0.1:3000 only"]
        end
        V1[("alias-index volume<br/>/data/index")]
        V2[("uploads volume<br/>/data/uploads")]
        V3[("caddy-data, caddy-config")]
        V4[("prometheus, grafana volumes<br/>(planned)")]
        DL["~/entity-resolver-deploy.log"]
    end
    subgraph External["Outside services"]
        WD[Wikidata API]
        GL[GLEIF API]
        GH["GHCR<br/>ghcr.io/normangw/entity-resolver"]
        CI[GitHub Actions]
    end
    U1 -->|"HTTPS, T1-T5"| P
    U2 -- HTTPS --> P
    LE -->|"HTTP-01 on :80"| P
    P -->|"HTTP, dial T6, response T7"| API
    API -->|"HTTPS, T9-T11, breaker T13"| WD
    API -->|"HTTPS, T14 (off by default)"| GL
    PR -.->|"scrape /metrics, bearer key"| API
    GR -.-> PR
    API --- V1
    API --- V2
    P --- V3
    PR -.- V4
    CI -->|"build, test, push image"| GH
    GH -->|"deploy.sh pulls by version"| API
```

Only the proxy publishes ports. The api trusts the client address Caddy writes into
`X-Forwarded-For`. Grafana, once added, listens on loopback and is reached through
`ssh -L 3000:localhost:3000`.

## 3. State view: what is kept, where, and for how long

```mermaid
flowchart TB
    subgraph Memory["api process memory (lost on restart)"]
        M1[Alias index + RapidFuzz choices]
        M2[Wikidata answer cache, TTL 1 h]
        M3[Circuit breaker, rate-limit buckets, Wikidata semaphore]
        M4[GLEIF daily budget]
        M5[Upload job table, running tasks]
        M6[Metrics counters]
    end
    subgraph Disk["Docker volumes on the instance"]
        D1[("alias-index: companies.full.json + .meta.json<br/>read-only at run time")]
        D2[("uploads: input, result, meta.json, cache.json<br/>deleted after 1 h")]
        D3[("caddy-data: certificates, ACME account")]
    end
    subgraph Off["Off the instance"]
        B1["Backup archives (laptop, teammate's drive)<br/>alias-index-DATE.tar.gz + SHA-256 manifest"]
        B2["GHCR images, one per commit and release"]
        B3["Git repository: code, eval sets, seed index"]
    end
    D1 -->|"loaded once at startup"| M1
    D2 <-->|"saved on start, end, shutdown;<br/>reloaded at startup"| M5
    D1 -->|"backup_index.py backup, then scp"| B1
    B1 -->|"load_index.sh archive (restore)"| D1
    B2 -->|"deploy.sh VERSION / --previous"| M1
```

There is no database. The only state worth backing up is the alias index; uploads
are temporary on purpose, certificates can be issued again, and the image is
rebuilt from Git. Job files are read leniently (unknown fields ignored, missing
ones defaulted), so the old and new versions can read each other's files during a
rollback. Jobs expire after an hour, so no numbered migrations are needed.

## 4. Technical view: inside the api

```mermaid
flowchart LR
    subgraph ASGI["uvicorn event loop"]
        MW["Middleware: request id + access log,<br/>Cache-Control, body limit"]
        R1["/resolve, /resolve/batch"]
        R2["/upload/*: owner cookie check"]
        R3["/health, /ready, /metrics"]
    end
    subgraph Threads["worker threads (asyncio.to_thread)"]
        T1["Resolver.resolve (RapidFuzz)"]
        T2["WikidataClient.lookup<br/>retries, 429 wait, deadline"]
        T3["GLEIF fetch"]
        T4["tabular: read sheet, write and verify result"]
        T5["save job state"]
    end
    CFG["Settings (pydantic-settings)<br/>.env, validated at startup"]
    MW --> R1 & R2 & R3
    R1 --> T1
    R1 -->|"semaphore, T8"| T2
    R1 -->|"wait_for, T14"| T3
    R2 -->|"background task"| T4
    R2 --> T1 & T2 & T5
    CFG -.-> R1 & R2 & T2
```

| Part | Code | Notes |
|---|---|---|
| Web service | `main.py` | FastAPI app, routes, lifespan (loads the index in the background) |
| Settings | `config.py` | `ENVIRONMENT` profiles; bad values stop the app at startup |
| Matching | `resolver.py` | Normalization, RapidFuzz scorer (`SCORER`), tie rules, variants |
| Wikidata | `wikidata.py` | Search, entity fetch, parent and HQ rules, cache, breaker, deadline |
| GLEIF | `gleif.py` | LEI tiebreak, daily budget |
| Uploads | `uploads.py`, `tabular.py` | Job store, CSV/XLSX/XLS reading, formula neutralizing, output checks |
| Protection | `protection.py` | API key (header or bearer), rate limits, body limit, Cache-Control |
| Observability | `observability.py` | JSON logs with request id, metrics |
| Proxy | `Caddyfile` | TLS, security headers, body limits, `/metrics` 404, timeouts |
| Delivery | `.github/workflows/`, `scripts/deploy.sh` | CI builds and smoke-tests the image, pushes to GHCR; the server only pulls |

## 5. Where each time limit sits

```mermaid
flowchart LR
    B["Browser<br/>T1-T4: 5-120 s per request"] -->|"T5 headers 15 s"| C["Caddy"]
    C -->|"T6 dial 5 s<br/>T7 response 120 s"| A["api"]
    A -->|"T8 queue 5 s"| S["Wikidata slot"]
    S -->|"T9 deadline 15 s<br/>T10 call 5-10 s<br/>T11 retries, one 429 wait <= 2 s"| W["Wikidata"]
    A -->|"T12 batch budget 60 s"| A
    A -->|"T13 breaker 5 fails, 30 s"| W
    A -->|"T14 5 s"| G["GLEIF"]
    D["deploy.sh T19, monitor.py T20"] --> C
```

Each limit is above the slowest normal request for its hop and below the limit of
the hop in front of it: one Wikidata-backed name finishes within 25 s (production),
a batch within about 85 s, both under Caddy's 120 s, and the page waits 45 s for
one name. The table explains each value.
