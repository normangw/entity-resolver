import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from entity_resolver.config import settings
from entity_resolver.models import HealthResponse, ResolveRequest, ResolveResponse
from entity_resolver.resolver import Resolver


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the model once at startup; every request reuses it."""
    app.state.resolver = Resolver(
        settings.alias_index_path,
        threshold=settings.match_threshold,
        delay_seconds=settings.model_delay_seconds,
    )
    yield


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Check that the service is running."""
    return HealthResponse(status="ok", version=settings.app_version)


@app.post("/resolve", response_model=ResolveResponse)
async def resolve(body: ResolveRequest, request: Request) -> ResolveResponse:
    """Resolve a company name to its canonical entity."""
    resolver: Resolver = request.app.state.resolver
    # The model call is blocking, so hand it to a worker thread and keep the
    # event loop free for other requests (e.g. /health).
    match = await asyncio.to_thread(resolver.resolve, body.company_name)
    if match is None:
        return ResolveResponse(
            input=body.company_name,
            status="unresolved",
            wikidata_id=None,
            canonical_name=None,
            parent_company=None,
            headquarters=None,
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
        confidence=match.score,
    )
