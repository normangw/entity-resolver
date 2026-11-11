"""Abuse and cost protection: API key, per-client rate limits, body size cap.

All of it is configured in ``config.Settings`` and read per request, so the demo
runs open by default and an operator tightens it with environment variables.
"""

import json
import secrets
import threading
import time
from collections.abc import Callable

from fastapi import HTTPException, Request, status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from entity_resolver.config import settings
from entity_resolver.observability import metrics


class RateLimiter:
    """Token bucket per (scope, client): ``per_minute`` requests, refilled steadily.

    In memory and per process, which is right for the single container this runs in.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}
        self._last_prune = clock()

    def check(self, scope: str, client: str, per_minute: int) -> float:
        """Spend one token. Returns 0 if allowed, else seconds until one is free."""
        if per_minute <= 0:
            return 0.0
        now = self._clock()
        rate = per_minute / 60.0
        with self._lock:
            tokens, stamp = self._buckets.get((scope, client), (float(per_minute), now))
            tokens = min(float(per_minute), tokens + (now - stamp) * rate)
            if tokens >= 1.0:
                self._buckets[(scope, client)] = (tokens - 1.0, now)
                allowed = 0.0
            else:
                self._buckets[(scope, client)] = (tokens, now)
                allowed = (1.0 - tokens) / rate
            if now - self._last_prune > 300:  # forget clients idle for 5 minutes
                self._buckets = {
                    k: v for k, v in self._buckets.items() if now - v[1] < 300
                }
                self._last_prune = now
            return allowed


limiter = RateLimiter()


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(scope: str) -> Callable[[Request], None]:
    """A dependency that answers 429 with Retry-After when a client is too fast."""

    def dependency(request: Request) -> None:
        per_minute = {
            "resolve": settings.rate_limit_resolve_per_min,
            "batch": settings.rate_limit_batch_per_min,
            "upload": settings.rate_limit_upload_per_min,
        }[scope]
        wait = limiter.check(scope, _client(request), per_minute)
        if wait > 0:
            metrics.inc("rate_limited_total", scope=scope)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests. Slow down and retry shortly.",
                headers={"Retry-After": str(max(1, int(wait) + 1))},
            )

    return dependency


def api_key(strict: bool) -> Callable[[Request], None]:
    """A dependency checking X-API-Key.

    With no ``API_KEY`` configured nothing is checked. ``strict`` endpoints (batch,
    metrics) always need the key once it is set; the others only when
    ``API_KEY_PROTECT_ALL`` is on.
    """

    def dependency(request: Request) -> None:
        expected = settings.api_key
        if not expected or not (strict or settings.api_key_protect_all):
            return
        given = request.headers.get("x-api-key", "")
        if not secrets.compare_digest(given.encode(), expected.encode()):
            metrics.inc("auth_failures_total")
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "A valid X-API-Key header is required.",
            )

    return dependency


class BodyLimitMiddleware:
    """Refuse a JSON body over ``max_body_bytes`` before it is read into memory.

    Uploads (``/upload``) are exempt: they stream to disk under their own limit.
    Counts the bytes actually received, so a missing or lying Content-Length does
    not get around it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in ("POST", "PUT", "PATCH")
            or scope["path"] == "/upload"
        ):
            await self.app(scope, receive, send)
            return
        limit = settings.max_body_bytes
        declared = dict(scope["headers"]).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            await self._reject(send)
            return
        seen = 0
        rejected = False

        async def counting_receive() -> Message:
            nonlocal seen, rejected
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    rejected = True
                    # Stop the app reading more; it sees an empty, final body.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def guarded_send(message: Message) -> None:
            if rejected:
                return
            await send(message)

        await self.app(scope, counting_receive, guarded_send)
        if rejected:
            await self._reject(send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = json.dumps({"detail": "Request body is too large."}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
