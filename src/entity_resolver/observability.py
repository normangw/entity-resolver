"""Structured logs, request ids and in-process metrics.

Nothing here records what a user typed or what a spreadsheet contains: log lines
carry the request id, the route template, the status and the duration only.
"""

import contextvars
import json
import logging
import re
import sys
import time
import traceback
import uuid
from typing import Any

from loguru import logger as loguru_logger
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
resolution_context_var: contextvars.ContextVar[tuple[str, str]] = (
    contextvars.ContextVar("resolution_context", default=("none", "none"))
)
# Anything else is counted as OTHER so a client cannot invent metric labels.
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_ID_OK = re.compile(r"[A-Za-z0-9._-]{8,64}")


class InterceptHandler(logging.Handler):
    """Forward standard logging records to Loguru without private payloads."""

    def emit(self, record: logging.LogRecord) -> None:
        """Forward the log level and approved request metadata."""

        level: str | int

        try:
            level = loguru_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        fields = {
            "request_id": request_id_var.get(),
            "logger": record.name,
        }

        if record.name == "entity_resolver.access":
            for key in ("method", "path", "status", "duration_ms"):
                if hasattr(record, key):
                    fields[key] = getattr(record, key)
            source, scorer = resolution_context_var.get()
            if fields.get("path") == "/resolve":
                fields.update(source=source, scorer=scorer)
            event = "request"
        elif record.name == "entity_resolver.resolution" and record.msg == "resolution":
            source = getattr(record, "source", "none")
            scorer = getattr(record, "scorer", "none")
            fields["source"] = (
                source if source in {"local", "wikidata", "gleif"} else "none"
            )
            fields["scorer"] = scorer if scorer in {"ratio", "WRatio"} else "none"
            event = "resolution"
        else:
            # Our own messages never include user input (the privacy tests check
            # this), so the text and any traceback are kept for debugging.
            event = record.getMessage()

        loguru_logger.bind(**fields).opt(exception=record.exc_info).log(level, event)


def _json_sink(message: Any) -> None:
    """Write one privacy-safe JSON object per log line."""
    record = message.record
    extra = record["extra"]

    entry = {
        "ts": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": extra.get("logger", record["name"]),
        "msg": record["message"],
        "request_id": extra.get("request_id", request_id_var.get()),
    }

    for key in (
        "method",
        "path",
        "status",
        "duration_ms",
        "source",
        "scorer",
    ):
        if key in extra:
            entry[key] = extra[key]

    exc = record["exception"]
    if exc is not None and exc.type is not None:
        entry["exc"] = "".join(
            traceback.format_exception(exc.type, exc.value, exc.traceback)
        )

    sys.stderr.write(json.dumps(entry, ensure_ascii=False) + "\n")


def configure_logging(fmt: str = "json", level: str = "INFO") -> None:
    """Route application and Uvicorn logs through Loguru."""
    loguru_logger.remove()

    loguru_logger.add(
        _json_sink,
        level=level.upper(),
        format="{message}",
        backtrace=False,
        diagnose=False,
    )

    root = logging.getLogger()
    root.handlers[:] = [InterceptHandler()]
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error"):
        target = logging.getLogger(name)
        target.handlers[:] = []
        target.propagate = True

    # Middleware provides the access log without raw URLs.
    access = logging.getLogger("uvicorn.access")
    access.handlers[:] = []
    access.propagate = False


_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class Metrics:
    """Collect application metrics using the Prometheus client."""

    def __init__(self) -> None:
        """Register counters, histograms and the process uptime gauge."""
        self.registry = CollectorRegistry()
        self.started = time.monotonic()

        self._counters: dict[str, Counter] = {
            "http_requests_total": Counter(
                "http_requests_total",
                "Total HTTP requests",
                ["method", "path", "status"],
                registry=self.registry,
            ),
            "http_errors_total": Counter(
                "http_errors_total",
                "Total HTTP requests returning 4xx or 5xx",
                ["method", "path", "status"],
                registry=self.registry,
            ),
            "resolutions_total": Counter(
                "resolutions_total",
                "Entity resolution outcomes",
                ["source", "status"],
                registry=self.registry,
            ),
            "wikidata_busy_total": Counter(
                "wikidata_busy_total",
                "Wikidata requests rejected by the concurrency limit",
                registry=self.registry,
            ),
            "wikidata_unavailable_total": Counter(
                "wikidata_unavailable_total",
                "Unavailable Wikidata lookups",
                registry=self.registry,
            ),
            "rate_limited_total": Counter(
                "rate_limited_total",
                "Requests rejected by rate limiting",
                ["scope"],
                registry=self.registry,
            ),
            "auth_failures_total": Counter(
                "auth_failures_total",
                "Failed authentication attempts",
                registry=self.registry,
            ),
            "wikidata_breaker_opens_total": Counter(
                "wikidata_breaker_opens_total",
                "Wikidata circuit breaker opening events",
                registry=self.registry,
            ),
            "wikidata_cache_requests_total": Counter(
                "wikidata_cache_requests_total",
                "Wikidata cache hits and misses",
                ["result"],
                registry=self.registry,
            ),
        }

        for result in ("hit", "miss"):
            self._counters["wikidata_cache_requests_total"].labels(result=result)

        self._latency = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds",
            ["method", "path"],
            buckets=_BUCKETS,
            registry=self.registry,
        )

        self._wikidata_latency = Histogram(
            "wikidata_call_duration_seconds",
            "Wikidata API call duration in seconds",
            buckets=_BUCKETS,
            registry=self.registry,
        )

        self._uptime = Gauge(
            "process_uptime_seconds",
            "Process uptime in seconds",
            registry=self.registry,
        )
        self._uptime.set_function(lambda: time.monotonic() - self.started)

    def inc(self, name: str, **labels: str) -> None:
        """Increment a registered counter with approved labels."""
        counter = self._counters[name]
        if labels:
            counter.labels(**labels).inc()
        else:
            counter.inc()

    def observe(self, seconds: float, *, method: str, path: str) -> None:
        """Record HTTP latency for a method and route template."""
        self._latency.labels(method=method, path=path).observe(seconds)

    def observe_wikidata(self, seconds: float) -> None:
        """Record the duration of a Wikidata API call."""
        self._wikidata_latency.observe(seconds)

    def value(self, name: str, **labels: str) -> int:
        """Return the current counter value, or zero if absent."""
        value = self.registry.get_sample_value(name, labels)
        return int(value or 0)

    def render(self) -> str:
        """Export registered metrics in Prometheus text format."""
        return generate_latest(self.registry).decode("utf-8")


metrics = Metrics()
logger = logging.getLogger("entity_resolver.access")


class RequestMiddleware:
    """Assign a request id, time the request, count it and write one log line."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the application to record HTTP request metrics."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle an ASGI request and record observability metadata."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = ""
        for key, value in scope["headers"]:
            if key == b"x-request-id":
                incoming = value.decode("latin-1")

        rid = incoming if _ID_OK.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        resolution_token = resolution_context_var.set(("none", "none"))
        started = time.perf_counter()
        status = 500

        async def send_with_id(message: Message) -> None:
            """Forward a response with its request ID header."""
            nonlocal status

            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", rid.encode()))

            await send(message)

        try:
            await self.app(scope, receive, send_with_id)

        finally:
            try:
                elapsed = time.perf_counter() - started

                route = scope.get("route")
                path = getattr(route, "path", None) or "unmatched"

                method = scope["method"] if scope["method"] in _METHODS else "OTHER"

                # Count all HTTP requests.
                metrics.inc(
                    "http_requests_total",
                    method=method,
                    path=path,
                    status=str(status),
                )

                # Count HTTP errors separately.
                if status >= 400:
                    metrics.inc(
                        "http_errors_total",
                        method=method,
                        path=path,
                        status=str(status),
                    )

                # Record latency by route and method.
                metrics.observe(
                    elapsed,
                    method=method,
                    path=path,
                )

                # Record privacy-safe structured access logs.
                logger.info(
                    "request",
                    extra={
                        "method": method,
                        "path": path,
                        "status": status,
                        "duration_ms": round(elapsed * 1000, 1),
                    },
                )
            finally:
                resolution_context_var.reset(resolution_token)
                request_id_var.reset(token)
