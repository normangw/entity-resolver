from typing import Literal

from pydantic import BaseModel, Field, field_validator


class HealthResponse(BaseModel):
    """Response model for the /health endpoint."""

    status: str
    version: str


class ReadyResponse(BaseModel):
    """Response model for the /ready endpoint."""

    status: str  # "ready" | "not ready"


class ResolveRequest(BaseModel):
    """Request model for company entity resolution."""

    company_name: str = Field(min_length=1, max_length=200)
    language: str = Field(default="en", pattern=r"^[a-z]{2}$")
    region: Literal["world", "us", "cn", "gb", "jp", "eu"] = "world"

    @field_validator("company_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("company_name must not be blank")
        return v.strip()


class ResolveResponse(BaseModel):
    """Response model for company entity resolution."""

    input: str
    status: str  # "resolved" | "unresolved"
    wikidata_id: str | None
    canonical_name: str | None
    parent_company: str | None
    headquarters: str | None
    country: str | None = None
    confidence: float | None
