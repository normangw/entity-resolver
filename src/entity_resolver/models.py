from typing import Literal

from pydantic import BaseModel, Field, field_validator


class HealthResponse(BaseModel):
    """Response model for the /health endpoint."""

    status: str
    version: str


class ReadyResponse(BaseModel):
    """Response model for the /ready endpoint."""

    status: str  # "ready" | "not ready"


Region = Literal["world", "us", "cn", "gb", "jp", "eu"]


class ResolveRequest(BaseModel):
    """Request model for company entity resolution."""

    company_name: str = Field(min_length=1, max_length=200)
    language: str = Field(default="en", pattern=r"^[a-z]{2}$")
    region: Region = "world"

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
    source: Literal["local", "gleif"] | None = None
    confidence: float | None


class UploadResponse(BaseModel):
    """What the service learned about a freshly uploaded spreadsheet."""

    id: str
    filename: str
    kind: Literal["xlsx", "xls", "csv"]
    sheets: list[str]  # empty for a csv
    max_rows: int


class PreviewResponse(BaseModel):
    """Column labels and the first few rows of one sheet, to pick the name column."""

    columns: list[str]
    rows: list[list[str]]
    suggested_column: int


class UploadRunRequest(BaseModel):
    """Which sheet and column hold the company names, and where to look them up."""

    sheet: str | None = None
    has_header: bool = True
    column: int = Field(ge=0, le=16383)  # 16384 is Excel's column limit
    region: Region = "world"


class Check(BaseModel):
    """One integrity check on the resolved file."""

    name: str
    ok: bool
    detail: str


class SampleRow(BaseModel):
    input: str
    status: str
    canonical_name: str | None
    country: str | None
    confidence: float | None


class UploadSummary(BaseModel):
    """Counts, integrity checks and a sample of a finished upload job."""

    rows: int
    unique_names: int
    resolved: int
    unresolved: int
    blank: int
    skipped: int
    from_local: int
    from_gleif: int
    checks: list[Check]
    passed: bool  # every check is ok
    sample: list[SampleRow]
    download_name: str


class UploadStatus(BaseModel):
    """Progress of a file's resolve job."""

    state: Literal["uploaded", "running", "done", "error"]
    done: int
    total: int
    error: str | None = None
    summary: UploadSummary | None = None
