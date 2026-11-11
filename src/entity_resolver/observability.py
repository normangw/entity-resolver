"""Structured logs, request ids and in-process metrics.

Nothing here records what a user typed or what a spreadsheet contains: log lines
carry the request id, the route template, the status and the duration only.
"""

import contextvars
import json
import logging
import re
import threading
import time
import uuid
from collections import defaultdict

from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
# Anything else is counted as OTHER so a client cannot invent metric labels.
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_ID_OK = re.compile(r"[A-Za-z0-9._-]{8,64}")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the request id of the current request."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for key in ("method", "path", "status", "duration_ms"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(fmt: str = "json", level: str = "INFO") -> None:
    """Send all logs, including uvicorn's, through one formatter on stderr."""
    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    # The request middleware writes the access log, without query strings.
    access = logging.getLogger("uvicorn.access")
    access.handlers[:] = []
    access.propagate = False


_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class Metrics:
    """Thread-safe counters and one latency histogram, rendered as Prometheus text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = (
            defaultdict(int)
        )
        self._latency = [0] * (len(_BUCKETS) + 1)
        self._latency_sum = 0.0
        self._latency_count = 0
        self.started = time.time()

    def inc(self, name: str, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] += 1

    def observe(self, seconds: float) -> None:
        with self._lock:
            self._latency_sum += seconds
            self._latency_count += 1
            for i, bound in enumerate(_BUCKETS):
                if seconds <= bound:
                    self._latency[i] += 1
                    return
            self._latency[-1] += 1

    def value(self, name: str, **labels: str) -> int:
        with self._lock:
            return self._counters.get((name, tuple(sorted(labels.items()))), 0)

    def render(self) -> str:
        with self._lock:
            lines: list[str] = []
            for (name, labels), count in sorted(self._counters.items()):
                tag = (
                    "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}"
                    if labels
                    else ""
                )
                lines.append(f"{name}{tag} {count}")
            cumulative = 0
            for bound, n in zip(_BUCKETS, self._latency, strict=False):
                cumulative += n
                lines.append(
                    f'http_request_duration_seconds_bucket{{le="{bound}"}} {cumulative}'
                )
            lines.append(
                'http_request_duration_seconds_bucket{le="+Inf"} '
                f"{self._latency_count}"
            )
            lines.append(f"http_request_duration_seconds_sum {self._latency_sum:.6f}")
            lines.append(f"http_request_duration_seconds_count {self._latency_count}")
            lines.append(f"process_uptime_seconds {time.time() - self.started:.0f}")
            return "\n".join(lines) + "\n"


metrics = Metrics()
logger = logging.getLogger("entity_resolver.access")


class RequestMiddleware:
    """Assign a request id, time the request, count it and write one log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = ""
        for key, value in scope["headers"]:
            if key == b"x-request-id":
                incoming = value.decode("latin-1")
        rid = incoming if _ID_OK.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.perf_counter()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", rid.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            elapsed = time.perf_counter() - started
            route = scope.get("route")
            path = getattr(route, "path", None) or "unmatched"
            method = scope["method"] if scope["method"] in _METHODS else "OTHER"
            metrics.inc(
                "http_requests_total",
                method=method,
                path=path,
                status=str(status),
            )
            metrics.observe(elapsed)
            logger.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": path,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 1),
                },
            )
            request_id_var.reset(token)
