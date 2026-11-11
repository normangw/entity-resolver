"""In-process load test of POST /resolve and /resolve/batch. Never touches the network.

Runs the real FastAPI app (middleware, rate limiting off, metrics on) through
Starlette's TestClient from many threads. Wikidata is replaced by a fake whose every
API call sleeps ``--wikidata-latency-ms``, so the concurrency cap and 503 path are
exercised without calling Wikimedia. The numbers measure this service on this
machine, not a network deployment: TestClient adds its own overhead, and real
Wikidata latency and throttling will dominate for names outside the index.

    PYTHONPATH=src uv run python scripts/loadtest.py --index data/companies.full.json
"""

import argparse
import os
import statistics
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def run(label: str, jobs: list[Any], workers: int, call: Any) -> None:
    latencies: list[float] = []
    codes: Counter[int] = Counter()
    lock = threading.Lock()

    def one(job: Any) -> None:
        started = time.perf_counter()
        code = call(job)
        elapsed = time.perf_counter() - started
        with lock:
            latencies.append(elapsed)
            codes[code] += 1

    began = time.perf_counter()
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, jobs))
    wall = time.perf_counter() - began
    print(
        f"{label:<34} n={len(jobs):<5} workers={workers:<3} "
        f"{len(jobs) / wall:7.1f} req/s  "
        f"p50={statistics.median(latencies) * 1000:7.1f} ms  "
        f"p95={percentile(latencies, 0.95) * 1000:7.1f} ms  "
        f"p99={percentile(latencies, 0.99) * 1000:7.1f} ms  "
        f"status={dict(sorted(codes.items()))}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--index", help="alias index (default: the bundled seed)")
    parser.add_argument("--requests", type=int, default=400)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--wikidata-latency-ms", type=float, default=50.0)
    parser.add_argument("--wikidata-slots", type=int, default=4)
    args = parser.parse_args()

    os.environ["WIKIDATA_FALLBACK"] = "true"
    os.environ["RATE_LIMIT_RESOLVE_PER_MIN"] = "0"
    os.environ["RATE_LIMIT_BATCH_PER_MIN"] = "0"
    os.environ["WIKIDATA_MAX_CONCURRENT"] = str(args.wikidata_slots)
    os.environ["LOG_LEVEL"] = "WARNING"
    if args.index:
        os.environ["ALIAS_INDEX_PATH"] = args.index

    from fastapi.testclient import TestClient

    from entity_resolver import main
    from entity_resolver.wikidata import WikidataClient

    delay = args.wikidata_latency_ms / 1000

    def fake_fetch(params: dict[str, str]) -> dict[str, Any]:
        time.sleep(delay)  # one Wikidata API call; a miss costs exactly one
        return {"search": []}

    main.WikidataClient = lambda *a, **k: WikidataClient(  # type: ignore[assignment]
        fetch=fake_fetch, cache_ttl=0
    )

    known = [
        "Apple Inc.",
        "Wal-Mart Stores Inc.",
        "Microsoft Corporation",
        "Siemens AG",
    ]
    with TestClient(main.app) as client:
        while client.get("/ready").status_code != 200:
            time.sleep(0.05)
        print(
            f"index={args.index or 'bundled seed'}  wikidata fake latency="
            f"{args.wikidata_latency_ms:g} ms/call  slots={args.wikidata_slots}"
        )

        def resolve(name: str) -> int:
            return client.post("/resolve", json={"company_name": name}).status_code

        run(
            "resolve: names in the index",
            [known[i % 4] for i in range(args.requests)],
            args.workers,
            resolve,
        )
        run(
            "resolve: misses, Wikidata fake",
            [f"Zyxwv Quantum Bakery {i}" for i in range(args.requests // 2)],
            args.workers,
            resolve,
        )

        def batch(i: int) -> int:
            names = [f"Zyxwv Quantum {i}-{j}" for j in range(20)] + known
            r = client.post("/resolve/batch", json={"company_names": names})
            return r.status_code

        run("batch: 24 names (20 misses)", list(range(20)), 8, batch)
        print(
            "metrics: wikidata_busy_total="
            f"{main.metrics.value('wikidata_busy_total')}  "
            "wikidata_unavailable_total="
            f"{main.metrics.value('wikidata_unavailable_total')}"
        )


if __name__ == "__main__":
    main()
