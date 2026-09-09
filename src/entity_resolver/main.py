from fastapi import FastAPI

from entity_resolver.config import settings
from entity_resolver.models import HealthResponse, ResolveRequest, ResolveResponse

app = FastAPI(title=settings.app_name, version=settings.app_version)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Check that the service is running."""
    return HealthResponse(status="ok", version=settings.app_version)


@app.post("/resolve", response_model=ResolveResponse)
def resolve(request: ResolveRequest) -> ResolveResponse:
    """Resolve a company name to its canonical entity (placeholder)."""
    return ResolveResponse(
        input=request.company_name,
        canonical_name=None,
        parent_company=None,
        headquarters=None,
        confidence=None,
    )
