"""First-line monitoring: an automated check that the deployed service still works.

Probes ``/health``, ``/ready`` and a few known names through ``POST /resolve``, times
them, and exits non-zero (printing what failed) so cron, a CI schedule or a person
notices before users do. Standard library only, so it runs anywhere.

    python scripts/monitor.py http://149.165.159.230
    python scripts/monitor.py http://host --samples 20 --p95-ms 1500 --webhook-env HOOK
    python scripts/monitor.py http://host --smoke eval/smoke.jsonl --expect-version 1.0

What it checks:
  * /health answers ok and reports a build id (shown, so you can see what is deployed)
  * /ready says ready
  * "Lyft" resolves and a made-up name stays unresolved (a wrong answer is as bad as
    no answer)
  * the p50 and p95 latency of those lookups stay under the limits
A 503 "Wikidata is busy" on the canary is reported as a warning, not a failure, since
that is the designed behavior under throttling.

With ``--smoke FILE`` the canaries are replaced by a fixed set of known-good answers,
one JSON object per line: ``company_name`` (plus optional ``region`` and ``language``)
is sent, and every other key is an expected field of the answer, for example
``{"company_name": "YouTube", "status": "resolved", "parent_company": "Google"}``.
Each case runs once. CI runs ``eval/smoke-seed.jsonl`` against the image it built and
``scripts/deploy.sh`` runs ``eval/smoke.jsonl`` after every deploy, so answers that get
worse are caught even when every status code is 200. ``--expect-version`` also checks
the version /health reports.

In smoke mode a wrong answer always fails. A 503 depends on the case: a case that
expects ``"source": "local"`` is answered from the alias index and must never be busy,
so a 503 there fails. Any other case may need Wikidata, whose throttling is answered
with a designed 503, so those are tried once more after ``--busy-retry-seconds`` (10)
and, if still busy, reported as a warning. ``--warn-exit-code`` lets a caller tell
"passed with warnings" from "passed" (deploy.sh logs it as WARN).

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

# Lyft is in the index; the other name must never resolve.
CANARIES: tuple[dict[str, str], ...] = (
    {"company_name": "Lyft", "status": "resolved"},
    {"company_name": "Zyxwv Quantum Bakery", "status": "unresolved"},
)
# Keys of a case that are sent with the request; every other key is an expectation.
REQUEST_KEYS = ("company_name", "region", "language")


def load_cases(path: str) -> list[dict[str, Any]]:
    """Read a smoke file: one JSON object per line; skip blanks and # comments."""
    cases = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            case = json.loads(line)
            if not case.get("company_name") or len(case) < 2:
                raise ValueError(
                    f"{path}:{number}: need company_name and an expectation"
                )
            cases.append(case)
    if not cases:
        raise ValueError(f"{path} has no cases")
    return cases


def mismatches(case: dict[str, Any], answer: dict[str, Any]) -> list[str]:
    """Each expected field of ``case`` that ``answer`` does not match."""
    return [
        f"{key} expected {want!r}, got {answer.get(key)!r}"
        for key, want in case.items()
        if key not in REQUEST_KEYS and answer.get(key) != want
    ]


def label(case: dict[str, Any]) -> str:
    """How a case is named in output: the name, plus region/language when given."""
    extra = [f"{k}={case[k]}" for k in REQUEST_KEYS[1:] if k in case]
    return repr(case["company_name"]) + (f" ({', '.join(extra)})" if extra else "")


def is_local(case: dict[str, Any]) -> bool:
    """Whether a smoke case is answered from the alias index (so never busy)."""
    return case.get("source") == "local"


def call(
    base: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30
) -> tuple[int, Any, float]:
    """One request: (HTTP status, parsed JSON or None, seconds taken).

    ``timeout`` (30 s) is above the server's worst case for one Wikidata-backed
    ``/resolve``: queue 5 s + lookup deadline 15 s + one call 10 s (see
    docs/time-limits.md), so a slow but working server is not reported as down.
    """
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
    """The ``q`` quantile (0-1) of ``values`` by nearest rank."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def check(
    base: str,
    samples: int,
    p50_ms: float,
    p95_ms: float,
    cases: list[dict[str, Any]] | None = None,
    expect_version: str | None = None,
    busy_retry_seconds: float = 10,
) -> tuple[list[str], list[str], list[str]]:
    """Run every check; returns (failures, warnings, info lines).

    Without ``cases`` the canaries are cycled ``samples`` times; with ``cases`` each
    one runs once, and busy (503) cases are retried once (see the module docstring).
    """
    failures: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    try:
        status, body, _ = call(base, "/health")
        if status != 200 or not body or body.get("status") != "ok":
            failures.append(f"/health returned {status}")
        else:
            info.append(f"version {body.get('version')}, build {body.get('build')}")
            if expect_version and body.get("version") != expect_version:
                failures.append(
                    f"/health reports version {body.get('version')}, "
                    f"expected {expect_version}"
                )
        status, body, _ = call(base, "/ready")
        if status != 200:
            failures.append(f"/ready returned {status} (not ready)")
    except OSError as exc:
        return [f"cannot reach {base}: {exc}"], warnings, info

    if cases is None:
        run = [CANARIES[i % len(CANARIES)] for i in range(samples)]
    else:
        run = cases
    times: list[float] = []

    def resolve_all(batch: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Check each case; return the busy ones, or None if the service is gone."""
        busy_cases = []
        for case in batch:
            name = label(case)
            request = {k: case[k] for k in REQUEST_KEYS if k in case}
            try:
                status, body, elapsed = call(base, "/resolve", request)
            except OSError as exc:
                failures.append(f"/resolve failed: {exc}")
                return None
            if status == 503:
                busy_cases.append(case)
            elif status != 200 or not isinstance(body, dict):
                failures.append(f"{name}: HTTP {status}")
            elif wrong := mismatches(case, body):
                failures.append(f"{name}: {'; '.join(wrong)}")
            else:
                times.append(elapsed)
        return busy_cases

    busy = resolve_all(run)
    if busy and cases is not None:
        time.sleep(busy_retry_seconds)
        busy = resolve_all(busy)
    if cases is None:
        if busy:
            warnings.append(
                f"{len(busy)} of {len(run)} lookups got 503 (Wikidata busy?)"
            )
    else:
        for case in busy or []:
            if is_local(case):
                failures.append(f"{label(case)}: 503 on a local-index answer")
            else:
                warnings.append(f"{label(case)}: 503 twice (Wikidata busy?)")
    if times:
        p50 = statistics.median(times) * 1000
        p95 = percentile(times, 0.95) * 1000
        info.append(f"latency p50 {p50:.0f} ms, p95 {p95:.0f} ms over {len(times)}")
        if p50 > p50_ms:
            failures.append(f"p50 {p50:.0f} ms is over the {p50_ms:.0f} ms limit")
        if p95 > p95_ms:
            failures.append(f"p95 {p95:.0f} ms is over the {p95_ms:.0f} ms limit")
    elif not failures and cases is None:
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
    """Parse arguments, run the checks, print the result; 1 means something failed."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("url", help="base URL, e.g. http://149.165.159.230")
    parser.add_argument("--samples", type=int, default=6)
    parser.add_argument("--p50-ms", type=float, default=1000)
    parser.add_argument("--p95-ms", type=float, default=3000)
    parser.add_argument(
        "--smoke",
        metavar="FILE",
        help="JSON-lines file of known-good answers to check instead of the canaries",
    )
    parser.add_argument(
        "--expect-version", help="fail unless /health reports this version"
    )
    parser.add_argument(
        "--busy-retry-seconds",
        type=float,
        default=10,
        help="smoke mode: wait this long, then retry cases that got 503",
    )
    parser.add_argument(
        "--warn-exit-code",
        type=int,
        default=0,
        help="exit code when every check passed but there were warnings",
    )
    parser.add_argument(
        "--webhook-env",
        help="environment variable holding a webhook URL to post failures to",
    )
    args = parser.parse_args()
    cases = load_cases(args.smoke) if args.smoke else None
    failures, warnings, info = check(
        args.url,
        args.samples,
        args.p50_ms,
        args.p95_ms,
        cases,
        args.expect_version,
        args.busy_retry_seconds,
    )
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
    if warnings:
        print("ok, with warnings")
        return args.warn_exit_code
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
