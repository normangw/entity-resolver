"""Send N concurrent /resolve requests and report wall-clock and slowest latency.

Usage: uv run python scripts/bench.py [BASE_URL] [N]
"""

import asyncio
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 10


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=120) as client:

        async def one() -> float:
            t0 = time.monotonic()
            r = await client.post("/resolve", json={"company_name": "Walmart"})
            r.raise_for_status()
            return time.monotonic() - t0

        t0 = time.monotonic()
        latencies = await asyncio.gather(*(one() for _ in range(N)))
        total = time.monotonic() - t0
    print(f"{N} requests took {total:.1f} s; slowest request {max(latencies):.1f} s")


asyncio.run(main())
