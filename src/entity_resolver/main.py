import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse

from entity_resolver import tabular, uploads
from entity_resolver.config import settings
from entity_resolver.gleif import Budget, HttpGleifClient, Verified, tiebreak
from entity_resolver.models import (
    HealthResponse,
    PreviewResponse,
    ReadyResponse,
    ResolveRequest,
    ResolveResponse,
    UploadResponse,
    UploadRunRequest,
    UploadStatus,
)
from entity_resolver.resolver import Resolver

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


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
    app.state.jobs = uploads.JobStore(
        settings.upload_dir, settings.upload_ttl_seconds, settings.upload_max_running
    )
    app.state.jobs.sweep()
    load_task = asyncio.create_task(_load_into(app))
    try:
        yield
    finally:
        load_task.cancel()
        app.state.jobs.close()


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Serve the single-page web UI, which calls POST /resolve."""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


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


async def _escalate(state, resolver: Resolver, name: str, region: str):
    """Ask GLEIF to settle a low-confidence match; None if it cannot."""
    client = state.gleif
    if client is None:
        return None
    candidates = await asyncio.to_thread(
        resolver.candidates, name, region, settings.gleif_low_threshold
    )
    try:
        return await asyncio.wait_for(
            tiebreak(
                name,
                candidates,
                client,
                settings.match_threshold,
                state.gleif_budget,
            ),
            timeout=settings.gleif_timeout,
        )
    except Exception:
        # An unavailable second opinion must not fail the request.
        logger.exception("GLEIF tiebreak failed")
        return None


async def resolve_name(state, name: str, region: str) -> ResolveResponse:
    """Resolve one name with the loaded resolver, then GLEIF if it is unsure.

    Shared by POST /resolve and the file-upload jobs. ``state`` is ``app.state``.
    """
    resolver: Resolver = state.resolver
    # The model call is blocking, so hand it to a worker thread and keep the
    # event loop free for other requests (e.g. /health).
    match = await asyncio.to_thread(resolver.resolve, name, region)
    if match is None:
        verified: Verified | None = await _escalate(state, resolver, name, region)
        if verified is not None:
            e = verified.match.entity
            return ResolveResponse(
                input=name,
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
            input=name,
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
        input=name,
        status="resolved",
        wikidata_id=e.wikidata_id,
        canonical_name=e.canonical_name,
        parent_company=e.parent_company,
        headquarters=e.headquarters,
        country=e.country,
        source="local",
        confidence=match.score,
    )


@app.post("/resolve", response_model=ResolveResponse)
async def resolve(body: ResolveRequest, request: Request) -> ResolveResponse:
    """Resolve a company name to its canonical entity."""
    if request.app.state.resolver is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model is still loading",
        )
    return await resolve_name(request.app.state, body.company_name, body.region)


# --- Spreadsheet upload -----------------------------------------------------
# POST /upload saves and inspects the file; GET .../preview shows its columns;
# POST .../resolve starts a background job on the chosen sheet and column;
# GET .../status reports progress and the integrity checks; GET .../download
# returns the original rows with the resolver's columns appended.


def _job(request: Request, job_id: str) -> uploads.Job:
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload not found or expired")
    return job


def _bad_file(exc: tabular.UploadError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@app.post("/upload", response_model=UploadResponse)
async def upload(file: UploadFile, request: Request) -> UploadResponse:
    """Upload an .xlsx, .xls or .csv file. Nothing is resolved yet."""
    store: uploads.JobStore = request.app.state.jobs
    store.sweep()
    filename = Path(file.filename or "upload").name
    ext = Path(filename).suffix.lower()
    job = store.create(filename, ext if ext in tabular.ALLOWED_EXTENSIONS else ".bin")
    try:
        size = 0
        with job.input_path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.upload_max_bytes:
                    raise HTTPException(
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        f"File is larger than {settings.upload_max_bytes // 2**20} MB",
                    )
                out.write(chunk)
        kind = await asyncio.to_thread(tabular.detect_kind, job.input_path, filename)
        sheets = await asyncio.to_thread(tabular.sheet_names, job.input_path, kind)
    except tabular.UploadError as exc:
        store.discard(job.id)
        raise _bad_file(exc) from None
    except BaseException:
        store.discard(job.id)
        raise
    return UploadResponse(
        id=job.id,
        filename=filename,
        kind=kind,
        sheets=sheets,
        max_rows=settings.upload_max_rows,
    )


@app.get("/upload/{job_id}/preview", response_model=PreviewResponse)
async def upload_preview(
    job_id: str, request: Request, sheet: str | None = None, has_header: bool = True
) -> PreviewResponse:
    """Column labels and the first rows of a sheet, for choosing the name column."""
    job = _job(request, job_id)
    try:
        labels, rows, suggested = await asyncio.to_thread(
            uploads.preview, job, sheet, has_header
        )
    except tabular.UploadError as exc:
        raise _bad_file(exc) from None
    return PreviewResponse(columns=labels, rows=rows, suggested_column=suggested)


@app.post(
    "/upload/{job_id}/resolve",
    response_model=UploadStatus,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_resolve(
    job_id: str, body: UploadRunRequest, request: Request
) -> UploadStatus:
    """Start resolving the chosen column in the background."""
    state = request.app.state
    job = _job(request, job_id)
    if state.resolver is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Model is still loading"
        )
    if job.state == "running":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This file is already being resolved"
        )
    if state.jobs.running() >= state.jobs.max_running:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Other files are being resolved. Try again soon.",
        )
    try:
        table = await asyncio.to_thread(
            uploads.load_table,
            job,
            body.sheet,
            body.has_header,
            body.column,
            settings.upload_max_rows,
        )
    except tabular.UploadError as exc:
        raise _bad_file(exc) from None
    job.state, job.done, job.total = "running", 0, len(table.data)
    job.task = asyncio.create_task(
        uploads.process(job, table, lambda name: resolve_name(state, name, body.region))
    )
    return _job_status(job)


def _job_status(job: uploads.Job) -> UploadStatus:
    return UploadStatus(
        state=job.state,  # type: ignore[arg-type]
        done=job.done,
        total=job.total,
        error=job.error,
        summary=job.summary,
    )


@app.get("/upload/{job_id}/status", response_model=UploadStatus)
async def upload_status(job_id: str, request: Request) -> UploadStatus:
    """Progress of the resolve job, and the integrity checks once it is done."""
    return _job_status(_job(request, job_id))


@app.get("/upload/{job_id}/download")
async def upload_download(job_id: str, request: Request) -> FileResponse:
    """The resolved file: the original rows with the resolver's columns appended."""
    job = _job(request, job_id)
    if job.state != "done" or job.output is None or job.summary is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The file is not resolved yet")
    return FileResponse(job.output, filename=job.summary.download_name)
