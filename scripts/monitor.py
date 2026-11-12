"""First-line monitoring: an automated check that the deployed service still works.

Probes ``/health``, ``/ready`` and a few known names through ``POST /resolve``, times
them, and exits non-zero (printing what failed) so cron, a CI schedule or a person
notices before users do. Standard library only, so it runs anywhere.

    python scripts/monitor.py http://149.165.159.230
    python scripts/monitor.py http://host --samples 20 --p95-ms 1500 --webhook-env HOOK

What it checks:
  * /health answers ok and reports a build id (shown, so you can see what is deployed)
  * /ready says ready
  * "Lyft" resolves and a made-up name stays unresolved (a wrong answer is as bad as
    no answer)
  * the p50 and p95 latency of those lookups stay under the limits
A 503 "Wikidata is busy" on the canary is reported as a warning, not a failure, since
that is the designed behavior under throttling.

The Jetstream address changes whenever the instance is unshelved, so pass the current
one each time (or set the APP_URL variable the scheduled workflow reads).
"""

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# (name sent, status expected). Lyft is in the index; the other must never resolve.
CANARIES = (("Lyft", "resolved"), ("Zyxwv Quantum Bakery", "unresolved"))


def call(
    base: str, path: str, body: dict[str, Any] | None = None, timeout: float = 15
) -> tuple[int, Any, float]:
    """One request: (HTTP status, parsed JSON or None, seconds taken)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    elapsed = time.monotonic() - start
    try:
        return status, json.loads(raw), elapsed
    except ValueError:
        return status, None, elapsed


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def check(
    base: str, samples: int, p50_ms: float, p95_ms: float
) -> tuple[list[str], list[str], list[str]]:
    """Run every check; returns (failures, warnings, info lines)."""
    failures: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    try:
        status, body, _ = call(base, "/health")
        if status != 200 or not body or body.get("status") != "ok":
            failures.append(f"/health returned {status}")
        else:
            info.append(f"build {body.get('build', 'unknown')}")
        status, body, _ = call(base, "/ready")
        if status != 200:
            failures.append(f"/ready returned {status} (not ready)")
    except OSError as exc:
        return [f"cannot reach {base}: {exc}"], warnings, info

    times: list[float] = []
    busy = 0
    for i in range(samples):
        name, expected = CANARIES[i % len(CANARIES)]
        try:
            status, body, elapsed = call(base, "/resolve", {"company_name": name})
        except OSError as exc:
            failures.append(f"/resolve failed: {exc}")
            break
        if status == 503:
            busy += 1
            continue
        if status != 200 or not body or body.get("status") != expected:
            got = body.get("status") if body else status
            failures.append(f"{name!r}: expected {expected}, got {got}")
            continue
        times.append(elapsed)
    if busy:
        warnings.append(f"{busy} of {samples} lookups got 503 (Wikidata busy?)")
    if times:
        p50 = statistics.median(times) * 1000
        p95 = percentile(times, 0.95) * 1000
        info.append(f"latency p50 {p50:.0f} ms, p95 {p95:.0f} ms over {len(times)}")
        if p50 > p50_ms:
            failures.append(f"p50 {p50:.0f} ms is over the {p50_ms:.0f} ms limit")
        if p95 > p95_ms:
            failures.append(f"p95 {p95:.0f} ms is over the {p95_ms:.0f} ms limit")
    elif not failures:
        failures.append("no lookup completed")
    return failures, warnings, info


def notify(webhook: str, text: str) -> None:
    """Post ``text`` to a Slack-style webhook; never raises."""
    try:
        request = urllib.request.Request(
            webhook,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=10).close()
    except OSError as exc:
        print(f"could not notify: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("url", help="base URL, e.g. http://149.165.159.230")
    parser.add_argument("--samples", type=int, default=6)
    parser.add_argument("--p50-ms", type=float, default=1000)
    parser.add_argument("--p95-ms", type=float, default=3000)
    parser.add_argument(
        "--webhook-env",
        help="environment variable holding a webhook URL to post failures to",
    )
    args = parser.parse_args()
    failures, warnings, info = check(args.url, args.samples, args.p50_ms, args.p95_ms)
    for line in info:
        print(f"info: {line}")
    for line in warnings:
        print(f"warning: {line}")
    for line in failures:
        print(f"FAIL: {line}")
    if failures:
        hook = os.environ.get(args.webhook_env or "")
        if hook:
            notify(hook, f"entity-resolver monitor: {'; '.join(failures)}")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
