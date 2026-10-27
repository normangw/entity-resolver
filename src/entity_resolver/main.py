import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response, status

from entity_resolver.config import settings
from entity_resolver.gleif import Budget, HttpGleifClient, Verified, tiebreak
from entity_resolver.models import (
    HealthResponse,
    ReadyResponse,
    ResolveRequest,
    ResolveResponse,
)
from entity_resolver.resolver import Resolver

logger = logging.getLogger(__name__)


def _load_resolver() -> Resolver:
    return Resolver(
        settings.alias_index_path,
        threshold=settings.match_threshold,
        delay_seconds=settings.model_delay_seconds,
    )


async def _load_into(app: FastAPI) -> None:
    """Load the model off the event loop, then mark the service ready."""
    try:
        app.state.resolver = await asyncio.to_thread(_load_resolver)
    except Exception:
        # Stay alive but not ready, so /health shows the process is up
        # and /ready and /resolve keep returning 503.
        logger.exception("Failed to load the alias index")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start serving at once and load the model in the background.

    /health answers as soon as the process is up; /ready and /resolve return
    503 until the load finishes.
    """
    app.state.resolver = None
    app.state.gleif = (
        HttpGleifClient(timeout=settings.gleif_timeout)
        if settings.gleif_enabled
        else None
    )
    app.state.gleif_budget = Budget(settings.gleif_daily_budget)
    load_task = asyncio.create_task(_load_into(app))
    try:
        yield
    finally:
        load_task.cancel()


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Check that the service is running."""
    return HealthResponse(status="ok", version=settings.app_version)


@app.get("/ready", response_model=ReadyResponse)
async def ready(request: Request, response: Response) -> ReadyResponse:
    """Report whether the alias index has finished loading at startup."""
    if getattr(request.app.state, "resolver", None) is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(status="not ready")
    return ReadyResponse(status="ready")


async def _escalate(request: Request, resolver: Resolver, body: ResolveRequest):
    """Ask GLEIF to settle a low-confidence match; None if it cannot."""
    client = request.app.state.gleif
    if client is None:
        return None
    candidates = await asyncio.to_thread(
        resolver.candidates,
        body.company_name,
        body.region,
        settings.gleif_low_threshold,
    )
    try:
        return await asyncio.wait_for(
            tiebreak(
                body.company_name,
                candidates,
                client,
                settings.match_threshold,
                request.app.state.gleif_budget,
            ),
            timeout=settings.gleif_timeout,
        )
    except Exception:
        # An unavailable second opinion must not fail the request.
        logger.exception("GLEIF tiebreak failed")
        return None


@app.post("/resolve", response_model=ResolveResponse)
async def resolve(body: ResolveRequest, request: Request) -> ResolveResponse:
    """Resolve a company name to its canonical entity."""
    resolver: Resolver | None = request.app.state.resolver
    if resolver is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model is still loading",
        )
    # The model call is blocking, so hand it to a worker thread and keep the
    # event loop free for other requests (e.g. /health).
    match = await asyncio.to_thread(resolver.resolve, body.company_name, body.region)
    if match is None:
        verified: Verified | None = await _escalate(request, resolver, body)
        if verified is not None:
            e = verified.match.entity
            return ResolveResponse(
                input=body.company_name,
                status="resolved",
                wikidata_id=e.wikidata_id,
                canonical_name=e.canonical_name,
                parent_company=e.parent_company,
                headquarters=e.headquarters,
                country=e.country,
                source="gleif",
                confidence=verified.match.score,
            )
        return ResolveResponse(
            input=body.company_name,
            status="unresolved",
            wikidata_id=None,
            canonical_name=None,
            parent_company=None,
            headquarters=None,
            country=None,
            confidence=None,
        )
    e = match.entity
    return ResolveResponse(
        input=body.company_name,
        status="resolved",
        wikidata_id=e.wikidata_id,
        canonical_name=e.canonical_name,
        parent_company=e.parent_company,
        headquarters=e.headquarters,
        country=e.country,
        source="local",
        confidence=match.score,
    )
