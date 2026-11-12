import asyncio
import datetime as dt
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, PlainTextResponse

from entity_resolver import tabular, uploads
from entity_resolver.config import settings
from entity_resolver.gleif import Budget, HttpGleifClient, Verified, tiebreak
from entity_resolver.models import (
    BatchRequest,
    BatchResponse,
    HealthResponse,
    PreviewResponse,
    ReadyResponse,
    ResolveRequest,
    ResolveResponse,
    UploadResponse,
    UploadRunRequest,
    UploadStatus,
)
from entity_resolver.observability import (
    RequestMiddleware,
    configure_logging,
    metrics,
)
from entity_resolver.protection import BodyLimitMiddleware, api_key, rate_limit
from entity_resolver.resolver import Match, Resolver, is_generic, note_for, variants
from entity_resolver.wikidata import CircuitBreaker, WikidataClient, WikidataUnavailable

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
    configure_logging(settings.log_format, settings.log_level)
    app.state.resolver = None
    app.state.wikidata = (
        WikidataClient(
            settings.wikidata_timeout,
            settings.match_threshold,
            cache_ttl=settings.wikidata_cache_ttl,
            retries=settings.wikidata_retries,
            breaker=CircuitBreaker(
                settings.wikidata_breaker_failures, settings.wikidata_breaker_seconds
            ),
        )
        if settings.wikidata_fallback
        else None
    )
    # Caps Wikidata lookups in flight; waiting happens here, not in worker threads.
    app.state.wikidata_gate = asyncio.Semaphore(settings.wikidata_max_concurrent)
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
        await app.state.jobs.aclose()  # stop jobs and save where each one stopped


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
# Added last = outermost: the request id and timing cover the body-size check too.
app.add_middleware(BodyLimitMiddleware)
app.add_middleware(RequestMiddleware)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Serve the single-page web UI, which calls POST /resolve."""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Check that the service is running."""
    return HealthResponse(
        status="ok", version=settings.app_version, build=settings.build_id
    )


@app.get(
    "/metrics",
    response_class=PlainTextResponse,
    include_in_schema=False,
    dependencies=[Depends(api_key(True))],
)
async def metrics_endpoint() -> PlainTextResponse:
    """Counters and request latency in Prometheus text format."""
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@app.get("/ready", response_model=ReadyResponse)
async def ready(request: Request, response: Response) -> ReadyResponse:
    """Report whether the alias index has finished loading at startup."""
    if getattr(request.app.state, "resolver", None) is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(status="not ready")
    return ReadyResponse(status="ready")


_REVIEW_BELOW = 0.95  # scores between the threshold and this are flagged for review


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


async def _wikidata_lookup(
    state, wikidata: WikidataClient, candidate: str, language: str
) -> Match | None:
    """One Wikidata lookup under the global concurrency cap.

    A wait longer than ``WIKIDATA_QUEUE_SECONDS`` or an unavailable Wikidata (error,
    HTTP 429, open circuit breaker) is a 503, never "unresolved".
    """
    unavailable = HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Wikidata is unavailable; retry shortly",
    )
    gate: asyncio.Semaphore | None = getattr(state, "wikidata_gate", None)
    if gate is not None:
        try:
            await asyncio.wait_for(gate.acquire(), settings.wikidata_queue_seconds)
        except TimeoutError:
            metrics.inc("wikidata_busy_total")
            raise unavailable from None
    try:
        return await asyncio.to_thread(wikidata.lookup, candidate, language)
    except WikidataUnavailable as exc:
        metrics.inc("wikidata_unavailable_total")
        raise unavailable from exc
    finally:
        if gate is not None:
            gate.release()


async def resolve_name(
    state, name: str, region: str, language: str = "en"
) -> ResolveResponse:
    """``_resolve_name`` plus counters for how each answer was reached."""
    try:
        result = await _resolve_name(state, name, region, language)
    except HTTPException as exc:
        metrics.inc("resolutions_total", source="none", status=str(exc.status_code))
        raise
    metrics.inc(
        "resolutions_total", source=result.source or "none", status=result.status
    )
    return result


async def _resolve_name(
    state, name: str, region: str, language: str = "en"
) -> ResolveResponse:
    """Resolve one name with the loaded resolver, then GLEIF if it is unsure.

    Shared by POST /resolve and the file-upload jobs. ``state`` is ``app.state``.
    """
    resolver: Resolver = state.resolver
    wikidata: WikidataClient | None = getattr(state, "wikidata", None)
    today = dt.date.today().isoformat()
    match = None
    source: Literal["local", "wikidata"] = "local"
    used = name
    # Try the name as typed, then without vendor numbers, parentheses and a trailing
    # city, then (for "X d/b/a Y") each part. The first variant that resolves wins.
    for candidate in variants(name):
        # The model call is blocking, so hand it to a worker thread and keep the
        # event loop free for other requests (e.g. /health).
        match = await asyncio.to_thread(resolver.resolve, candidate, region)
        source = "local"
        # Wikidata hits carry no country, so they cannot honor a region filter.
        if (
            match is None
            and wikidata is not None
            and region == "world"
            and not is_generic(candidate)  # "Bank" or "Inc." finds an unrelated hit
        ):
            match = await _wikidata_lookup(state, wikidata, candidate, language)
            source = "wikidata"
        if match is not None:
            used = candidate
            break
    if match is None:
        verified: Verified | None = await _escalate(state, resolver, name, region)
        if verified is not None:
            e = verified.match.entity
            return ResolveResponse(
                input=name,
                status="resolved",
                wikidata_id=e.wikidata_id,
                canonical_name=e.canonical_name,
                label=e.label,
                parent_company=e.parent_company,
                headquarters=e.headquarters,
                country=e.country,
                source="gleif",
                confidence=verified.match.score,
                as_of=resolver.as_of,
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
            source=None,
        )
    e = match.entity
    note = note_for(used)
    return ResolveResponse(
        input=name,
        status="resolved",
        wikidata_id=e.wikidata_id,
        canonical_name=e.canonical_name,
        label=e.label,
        parent_company=e.parent_company,
        headquarters=e.headquarters,
        country=e.country,
        confidence=match.score,
        source=source,
        needs_review=(
            match.ambiguous or match.score < _REVIEW_BELOW or note is not None
        ),
        matched_alias=match.matched_alias
        or (e.label or e.canonical_name if source == "wikidata" else None),
        as_of=today if source == "wikidata" else resolver.as_of,
        note=note,
    )


def _retry_response(name: str) -> ResolveResponse:
    """A row whose lookup was not completed; the caller should send it again."""
    return ResolveResponse(
        input=name,
        status="retry",
        wikidata_id=None,
        canonical_name=None,
        parent_company=None,
        headquarters=None,
        country=None,
        confidence=None,
        source=None,
    )


def _upload_resolver(state, region: str, language: str = "en"):
    """A resolve function for upload and batch jobs that survives a busy Wikidata.

    This function never retries a lookup: the Wikidata client is the one place that
    retries (with backoff and jitter) and it has a circuit breaker. A name whose
    lookup still fails is marked ``retry`` so the caller can send it again later.
    After three such rows, names the local index cannot settle are marked ``retry``
    at once instead of waiting on Wikidata, so local matches are never lost and the
    job finishes.
    """
    failures = 0

    async def resolve_one(name: str) -> ResolveResponse:
        nonlocal failures
        if failures >= 3:
            local = await asyncio.to_thread(state.resolver.resolve, name, region)
            if local is None:
                return _retry_response(name)
        try:
            return await resolve_name(state, name, region, language)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
                raise
        failures += 1
        return _retry_response(name)

    return resolve_one


@app.post(
    "/resolve",
    response_model=ResolveResponse,
    dependencies=[Depends(api_key(False)), Depends(rate_limit("resolve"))],
)
async def resolve(body: ResolveRequest, request: Request) -> ResolveResponse:
    """Resolve a company name to its canonical entity."""
    if request.app.state.resolver is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model is still loading",
        )
    return await resolve_name(
        request.app.state, body.company_name, body.region, body.language
    )


@app.post(
    "/resolve/batch",
    response_model=BatchResponse,
    dependencies=[Depends(api_key(True)), Depends(rate_limit("batch"))],
)
async def resolve_batch(body: BatchRequest, request: Request) -> BatchResponse:
    """Resolve up to ``BATCH_MAX_NAMES`` names in one call.

    Results come back in input order; each distinct name is resolved once. A name
    whose Wikidata lookup is unavailable comes back with ``status: "retry"`` (send it
    again later) instead of failing the whole call.
    """
    state = request.app.state
    if state.resolver is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Model is still loading"
        )
    if len(body.company_names) > settings.batch_max_names:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"At most {settings.batch_max_names} names per call.",
        )
    resolve_one = _upload_resolver(state, body.region, body.language)
    answers: dict[str, ResolveResponse] = {}
    deadline = time.monotonic() + settings.batch_budget_seconds
    for name in body.company_names:
        if name in answers:
            continue
        if time.monotonic() > deadline:
            # Out of time: keep what the local index can settle, retry the rest.
            local = await asyncio.to_thread(state.resolver.resolve, name, body.region)
            if local is None:
                answers[name] = _retry_response(name)
                continue
        answers[name] = await resolve_one(name)
    results = [answers[name] for name in body.company_names]
    return BatchResponse(
        results=results,
        unique_names=len(answers),
        retry=sum(1 for r in results if r.status == "retry"),
    )


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


@app.post(
    "/upload",
    response_model=UploadResponse,
    dependencies=[Depends(api_key(False)), Depends(rate_limit("upload"))],
)
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


@app.get(
    "/upload/{job_id}/preview",
    response_model=PreviewResponse,
    dependencies=[Depends(api_key(False))],
)
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
    except Exception:
        logger.exception("Preview failed")
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "This spreadsheet could not be read."
        ) from None
    return PreviewResponse(columns=labels, rows=rows, suggested_column=suggested)


@app.post(
    "/upload/{job_id}/resolve",
    response_model=UploadStatus,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(api_key(False)), Depends(rate_limit("upload"))],
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
    except Exception:
        logger.exception("Could not load the uploaded table")
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "This spreadsheet could not be read."
        ) from None
    job.state, job.done, job.total = "running", 0, len(table.data)
    state.jobs.save(job)
    job.task = asyncio.create_task(
        uploads.process(job, table, _upload_resolver(state, body.region), body.region)
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


@app.get(
    "/upload/{job_id}/status",
    response_model=UploadStatus,
    dependencies=[Depends(api_key(False))],
)
async def upload_status(job_id: str, request: Request) -> UploadStatus:
    """Progress of the resolve job, and the integrity checks once it is done."""
    return _job_status(_job(request, job_id))


@app.get("/upload/{job_id}/download", dependencies=[Depends(api_key(False))])
async def upload_download(job_id: str, request: Request) -> FileResponse:
    """The resolved file: the original rows with the resolver's columns appended."""
    job = _job(request, job_id)
    if job.state != "done" or job.output is None or job.summary is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The file is not resolved yet")
    return FileResponse(job.output, filename=job.summary.download_name)
