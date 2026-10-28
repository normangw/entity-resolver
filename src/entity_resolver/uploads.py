"""Resolve the company-name column of an uploaded spreadsheet.

``tabular`` reads and writes the files safely; this module decides what to do with
them. A job's life: the file is saved under ``upload_dir/<id>/``, the caller picks a
sheet and a name column (``load_table``), each unique name is resolved once
(``process``), the original rows are written back with the resolver's columns
appended, and the written file is read back and checked against the input
(``verify_output``) so a bug can never silently drop or alter a row.

Nothing here logs cell contents.
"""

import asyncio
import logging
import re
import shutil
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from entity_resolver import tabular
from entity_resolver.models import Check, ResolveResponse, SampleRow, UploadSummary

logger = logging.getLogger(__name__)

MAX_NAME_CHARS = 200  # the limit POST /resolve puts on a name
PREVIEW_ROWS = 6
SAMPLE_ROWS = 8
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_NAME_HINTS = ("company", "name", "firm", "organi", "entity", "employer", "customer")
_STATUSES = {"resolved", "unresolved", "blank", "skipped"}

Resolve = Callable[[str], Awaitable[ResolveResponse]]


@dataclass
class Job:
    id: str
    dir: Path
    filename: str  # as uploaded; only ever used for display and the download name
    kind: tabular.Kind
    input_path: Path
    created: float = field(default_factory=time.time)
    state: str = "uploaded"  # uploaded | running | done | error
    done: int = 0
    total: int = 0
    error: str | None = None
    summary: UploadSummary | None = None
    output: Path | None = None
    task: asyncio.Task[None] | None = None


class JobStore:
    """Jobs in memory, their files on disk, both dropped after ``ttl`` seconds."""

    def __init__(self, root: Path, ttl: int, max_running: int) -> None:
        self.root = root
        self.ttl = ttl
        self.max_running = max_running
        self._jobs: dict[str, Job] = {}
        root.mkdir(parents=True, exist_ok=True)

    def create(self, filename: str, ext: str) -> Job:
        """A new job with an empty directory; the caller writes ``job.input_path``."""
        job_id = uuid.uuid4().hex
        job_dir = self.root / job_id
        job_dir.mkdir()
        kind = tabular.ALLOWED_EXTENSIONS.get(ext, "csv")
        job = Job(job_id, job_dir, filename, kind, job_dir / f"input{ext}")
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id) if _JOB_ID.fullmatch(job_id) else None

    def running(self) -> int:
        return sum(1 for job in self._jobs.values() if job.state == "running")

    def discard(self, job_id: str) -> None:
        job = self._jobs.pop(job_id, None)
        if job is not None and job.task is not None:
            job.task.cancel()
        shutil.rmtree(self.root / job_id, ignore_errors=True)

    def sweep(self) -> None:
        """Delete expired jobs and old job directories left by an earlier process."""
        cutoff = time.time() - self.ttl
        for job in list(self._jobs.values()):
            if job.state != "running" and job.created < cutoff:
                self.discard(job.id)
        # Only directories named like a job id: upload_dir may be shared.
        for entry in self.root.iterdir():
            if (
                entry.is_dir()
                and _JOB_ID.fullmatch(entry.name)
                and entry.name not in self._jobs
                and entry.stat().st_mtime < cutoff
            ):
                shutil.rmtree(entry, ignore_errors=True)

    def close(self) -> None:
        for job in self._jobs.values():
            if job.task is not None:
                job.task.cancel()


@dataclass(frozen=True)
class Table:
    labels: list[str]
    data: list[list[Any]]  # data rows only, padded to one width
    column: int  # index of the company-name column
    sheet_title: str


def _sheet_arg(path: Path, kind: tabular.Kind, sheet: str | None) -> str | None:
    """The sheet to read: None for a csv or the first sheet; error if it is unknown."""
    if kind == "csv" or sheet is None:
        return None
    if sheet not in tabular.sheet_names(path, kind):
        raise tabular.UploadError("That sheet is not in this file.")
    return sheet


def suggest_column(labels: Sequence[str]) -> int:
    """The first column whose label looks like company names, else the first."""
    for i, label in enumerate(labels):
        if any(hint in label.lower() for hint in _NAME_HINTS):
            return i
    return 0


def preview(
    job: Job, sheet: str | None, has_header: bool
) -> tuple[list[str], list[list[str]], int]:
    """Column labels, the first rows as text, and the suggested name column."""
    rows = tabular.read_rows(
        job.input_path,
        job.kind,
        _sheet_arg(job.input_path, job.kind, sheet),
        PREVIEW_ROWS + 1,
    )
    labels = tabular.column_labels(rows, has_header)
    body = rows[1 if has_header else 0 :][:PREVIEW_ROWS]
    shown = [[tabular.cell_text(v)[:60] for v in row] for row in body]
    return labels, shown, suggest_column(labels) if has_header else 0


def load_table(
    job: Job, sheet: str | None, has_header: bool, column: int, max_rows: int
) -> Table:
    """Read the whole sheet and validate the choices; raises ``UploadError``."""
    chosen = _sheet_arg(job.input_path, job.kind, sheet)
    rows = tabular.read_rows(job.input_path, job.kind, chosen, max_rows + 2)
    data = rows[1 if has_header else 0 :]
    if not data:
        raise tabular.UploadError("This sheet has no data rows.")
    if len(data) > max_rows:
        raise tabular.UploadError(
            f"This sheet has more than {max_rows:,} data rows. "
            "Split it and upload again."
        )
    if column >= len(rows[0]):
        raise tabular.UploadError("The chosen column does not exist.")
    return Table(
        labels=tabular.column_labels(rows, has_header),
        data=data,
        column=column,
        sheet_title=chosen or "Resolved",
    )


def _extra(r: ResolveResponse | None, status: str) -> list[Any]:
    """The ``RESULT_COLUMNS`` values for one row."""
    if r is None:
        return [status, None, None, None, None, None, None, None]
    return [
        r.status,
        r.canonical_name,
        r.wikidata_id,
        r.parent_company,
        r.headquarters,
        r.country,
        r.confidence,
        r.source,
    ]


def _expected(value: Any, kind: str) -> str:
    """The text a cell should have after being written (and read back) as ``kind``."""
    if kind == "csv":
        return tabular.neutralize(tabular.cell_text(value))
    if isinstance(value, str):
        value = tabular._XML_ILLEGAL.sub("", value)
    return tabular.cell_text(value)


def verify_output(
    path: Path, out_kind: str, table: Table, statuses: list[str]
) -> list[Check]:
    """Read the written file back and compare it with the input.

    Original cells must be untouched, no row added or lost, every row must carry
    a status, and every resolved row must be complete.
    """
    n = len(table.data)
    width = len(table.labels)
    out = tabular.read_rows(path, out_kind, None, n + 2)
    header, body = (out[0] if out else []), out[1:]
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append(Check(name=name, ok=ok, detail=detail))

    add(
        "Row count",
        len(body) == n,
        f"{n} rows in, {len(body)} rows out",
    )

    expected_head = [_expected(h, out_kind) for h in table.labels]
    add(
        "Original headers kept",
        [tabular.cell_text(h) for h in header[:width]] == expected_head,
        f"{width} original columns",
    )

    bad = 0
    for orig, new in zip(table.data, body, strict=False):
        for i in range(width):
            if _expected(orig[i], out_kind) != tabular.cell_text(new[i]):
                bad += 1
    add(
        "Original cells unchanged",
        bad == 0 and len(body) == n,
        "every original cell matches" if bad == 0 else f"{bad} cells differ",
    )

    col = table.column
    names_in = Counter(_expected(r[col], out_kind) for r in table.data)
    names_out = Counter(tabular.cell_text(r[col]) for r in body)
    missing = sum((names_in - names_out).values())
    add(
        "No company name lost",
        missing == 0,
        "all names present" if missing == 0 else f"{missing} names missing or changed",
    )

    status_col = width  # RESULT_COLUMNS[0]
    got = [tabular.cell_text(r[status_col]) for r in body]
    add(
        "Every row has a status",
        len(got) == n and all(s in _STATUSES for s in got) and got == statuses,
        "resolved, unresolved, blank or skipped on every row",
    )

    incomplete = 0
    for r in body:
        if tabular.cell_text(r[status_col]) != "resolved":
            continue
        canonical = tabular.cell_text(r[status_col + 1])
        wikidata = tabular.cell_text(r[status_col + 2])
        try:
            confidence = float(tabular.cell_text(r[status_col + 6]))
        except ValueError:
            confidence = -1.0
        if not canonical or not wikidata or not 0.0 <= confidence <= 1.0:
            incomplete += 1
    add(
        "Resolved rows complete",
        incomplete == 0,
        "name, Wikidata ID and confidence on every resolved row"
        if incomplete == 0
        else f"{incomplete} resolved rows are missing a field",
    )
    return checks


async def process(job: Job, table: Table, resolve: Resolve) -> None:
    """Resolve every unique name, write the result file and verify it."""
    job.state, job.done, job.total = "running", 0, len(table.data)
    job.error = job.summary = job.output = None
    try:
        cache: dict[str, ResolveResponse] = {}
        extra: list[list[Any]] = []
        statuses: list[str] = []
        sample: list[SampleRow] = []
        counts: Counter[str] = Counter()
        for row in table.data:
            name = tabular.cell_text(row[table.column]).strip()
            if not name:
                extra.append(_extra(None, "blank"))
                statuses.append("blank")
            elif len(name) > MAX_NAME_CHARS:
                extra.append(_extra(None, "skipped"))
                statuses.append("skipped")
            else:
                if name not in cache:
                    cache[name] = await resolve(name)
                hit = cache[name]
                extra.append(_extra(hit, hit.status))
                statuses.append(hit.status)
                if hit.source:
                    counts[hit.source] += 1
                if len(sample) < SAMPLE_ROWS:
                    sample.append(
                        SampleRow(
                            input=name,
                            status=hit.status,
                            canonical_name=hit.canonical_name,
                            country=hit.country,
                            confidence=hit.confidence,
                        )
                    )
            counts[statuses[-1]] += 1
            job.done += 1

        out_kind = "csv" if job.kind == "csv" else "xlsx"
        output = job.dir / f"result.{out_kind}"
        await asyncio.to_thread(
            tabular.write_result,
            output,
            job.kind,
            table.sheet_title,
            table.labels,
            table.data,
            extra,
        )
        checks = await asyncio.to_thread(
            verify_output, output, out_kind, table, statuses
        )
        stem = Path(job.filename).stem or "result"
        job.summary = UploadSummary(
            rows=len(table.data),
            unique_names=len(cache),
            resolved=counts["resolved"],
            unresolved=counts["unresolved"],
            blank=counts["blank"],
            skipped=counts["skipped"],
            from_local=counts["local"],
            from_gleif=counts["gleif"],
            checks=checks,
            passed=all(c.ok for c in checks),
            sample=sample,
            download_name=f"{stem}_resolved.{out_kind}",
        )
        job.output = output
        job.state = "done"
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Upload job failed")
        job.state = "error"
        job.error = "Something went wrong while resolving this file."
