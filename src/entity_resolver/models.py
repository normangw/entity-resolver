from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response model for the /health endpoint."""

    status: str
    version: str


class ResolveRequest(BaseModel):
    """Request model for company entity resolution."""

    company_name: str
    language: str = "en"


class ResolveResponse(BaseModel):
    """Response model for company entity resolution."""

    input: str
    canonical_name: str | None
    parent_company: str | None
    headquarters: str | None
    confidence: float | None
