"""Read an uploaded spreadsheet safely and write the resolved copy.

Uploads are untrusted. The file type comes from the first bytes, never from the
extension alone (the two must agree); macro workbooks are refused; ``.xlsx`` files are
read in openpyxl's read-only mode, which never evaluates formulas (cached values only);
zip structure is checked before parsing so a zip bomb is rejected cheaply; and text
written back is neutralized so a cell cannot turn into a formula when opened in Excel.

Nothing here logs cell contents.
"""

import csv
import datetime as dt
import io
import re
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

import openpyxl
import xlrd
from openpyxl.cell import WriteOnlyCell
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException

Kind = Literal["xlsx", "xls", "csv"]

ALLOWED_EXTENSIONS: dict[str, Kind] = {".xlsx": "xlsx", ".xls": "xls", ".csv": "csv"}
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")

# Zip-bomb guard for .xlsx (a zip): limits on what the archive claims to hold.
MAX_ZIP_MEMBERS = 2000
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200

_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
_NUMBER = re.compile(r"[+-]?(\d[\d,]*\.?\d*|\.\d+)([eE][+-]?\d+)?%?")

RESULT_COLUMNS = (
    "Resolver status",
    "Canonical name",
    "Wikidata ID",
    "Parent company",
    "Headquarters",
    "Country",
    "Confidence",
    "Source",
)


class UploadError(ValueError):
    """A problem with the uploaded file. The message is safe to show the user."""


def detect_kind(path: Path, filename: str) -> Kind:
    """Return the file type, checking the extension against the file's own bytes."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadError(
            "Unsupported file type. Upload a .xlsx, .xls or .csv file "
            "(macro-enabled .xlsm is not accepted)."
        )
    kind = ALLOWED_EXTENSIONS[ext]
    with path.open("rb") as handle:
        head = handle.read(8192)
    if kind == "xlsx":
        if not head.startswith(_ZIP_MAGIC):
            raise UploadError("This does not look like a real .xlsx file.")
        _check_zip(path)
    elif kind == "xls":
        if not head.startswith(_OLE_MAGIC):
            raise UploadError("This does not look like a real .xls file.")
    else:
        if head.startswith((_ZIP_MAGIC, _OLE_MAGIC)) or b"\x00" in head:
            if not head.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 is text
                raise UploadError("This does not look like a text .csv file.")
    return kind


def _check_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = {info.filename for info in infos}
    except zipfile.BadZipFile:
        raise UploadError("This .xlsx file is corrupt.") from None
    if len(infos) > MAX_ZIP_MEMBERS:
        raise UploadError("This .xlsx file has an unusual structure.")
    if "xl/workbook.xml" not in names:
        raise UploadError("This does not look like a real .xlsx file.")
    if "xl/vbaProject.bin" in names:
        raise UploadError("Macro-enabled workbooks are not accepted.")
    total = sum(info.file_size for info in infos)
    compressed = max(1, sum(info.compress_size for info in infos))
    if total > MAX_UNCOMPRESSED_BYTES or total / compressed > MAX_COMPRESSION_RATIO:
        raise UploadError("This .xlsx file expands too much to be accepted.")


def _csv_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def sheet_names(path: Path, kind: Kind) -> list[str]:
    """Names of the sheets a user can pick from (empty for a csv)."""
    try:
        if kind == "xlsx":
            workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
            try:
                return list(workbook.sheetnames)
            finally:
                workbook.close()
        if kind == "xls":
            book = xlrd.open_workbook(path, on_demand=True)
            try:
                return list(book.sheet_names())
            finally:
                book.release_resources()
    except (zipfile.BadZipFile, InvalidFileException, xlrd.XLRDError, KeyError):
        raise UploadError("This spreadsheet could not be read.") from None
    return []


def _clean(value: Any) -> Any:
    """Cell value as a plain Python value (dates stay dates, errors become text)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _xls_rows(path: Path, sheet: str | None, limit: int) -> Iterator[list[Any]]:
    book = xlrd.open_workbook(path, on_demand=True)
    try:
        sh = book.sheet_by_name(sheet) if sheet else book.sheet_by_index(0)
        for r in range(min(sh.nrows, limit)):
            row: list[Any] = []
            for cell in sh.row(r):
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        row.append(
                            xlrd.xldate.xldate_as_datetime(cell.value, book.datemode)
                        )
                    except (xlrd.xldate.XLDateError, ValueError, OverflowError):
                        row.append(cell.value)
                elif cell.ctype == xlrd.XL_CELL_ERROR:
                    row.append("#ERROR")
                elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    row.append(None)
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    row.append(bool(cell.value))
                elif (
                    cell.ctype == xlrd.XL_CELL_NUMBER and float(cell.value).is_integer()
                ):
                    row.append(int(cell.value))
                else:
                    row.append(cell.value)
            yield row
    finally:
        book.release_resources()


def read_rows(path: Path, kind: Kind, sheet: str | None, limit: int) -> list[list[Any]]:
    """Up to ``limit`` rows of the chosen sheet, padded to a rectangle.

    Trailing empty rows are dropped. Row 1 is whatever the file's first row is; the
    caller decides whether it is a header.
    """
    rows: list[list[Any]] = []
    try:
        if kind == "csv":
            reader = csv.reader(io.StringIO(_csv_text(path), newline=""))
            for i, row in enumerate(reader):
                if i >= limit:
                    break
                rows.append(list(row))
        elif kind == "xlsx":
            workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
            try:
                ws = workbook[sheet] if sheet else workbook.worksheets[0]
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= limit:
                        break
                    rows.append([_clean(v) for v in row])
            finally:
                workbook.close()
        else:
            rows = list(_xls_rows(path, sheet, limit))
    except (
        KeyError,
        zipfile.BadZipFile,
        InvalidFileException,
        xlrd.XLRDError,
        csv.Error,
    ):
        raise UploadError("This spreadsheet could not be read.") from None
    while rows and all(v in (None, "") for v in rows[-1]):
        rows.pop()
    width = max((len(r) for r in rows), default=0)
    return [r + [None] * (width - len(r)) for r in rows]


def column_labels(rows: Sequence[Sequence[Any]], has_header: bool) -> list[str]:
    width = len(rows[0]) if rows else 0
    if has_header and rows:
        return [
            str(v).strip()
            if v not in (None, "")
            else f"Column {get_column_letter(i + 1)}"
            for i, v in enumerate(rows[0])
        ]
    return [f"Column {get_column_letter(i + 1)}" for i in range(width)]


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def neutralize(value: Any) -> Any:
    """Make a text value safe to open in a spreadsheet (no formula injection).

    Strings that start with = + - @ (or a tab or carriage return) get a leading
    apostrophe, unless they are plain numbers. Other values pass through.
    """
    if (
        isinstance(value, str)
        and value.startswith(_FORMULA_TRIGGERS)
        and not _NUMBER.fullmatch(value.strip())
    ):
        return "'" + value
    return value


_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def write_result(
    path: Path,
    kind: Kind,
    sheet_title: str,
    header: Sequence[str],
    rows: Sequence[Sequence[Any]],
    extra: Sequence[Sequence[Any]],
) -> None:
    """Write the original rows with the resolver's columns appended.

    ``header`` is the original column labels (generated if the file had none) and
    ``extra`` has one list of ``RESULT_COLUMNS`` values per row. A csv stays a csv; a
    workbook (including .xls, which cannot be written) comes back as .xlsx.
    """
    head = [*header, *RESULT_COLUMNS]
    if kind == "csv":
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([neutralize(h) for h in head])
            for row, add in zip(rows, extra, strict=True):
                writer.writerow(
                    [neutralize(cell_text(v)) for v in row]
                    + [neutralize(cell_text(v)) for v in add]
                )
        return
    workbook = openpyxl.Workbook(write_only=True)
    title = re.sub(r"[\[\]:*?/\\]", " ", sheet_title)[:31] or "Resolved"
    ws = workbook.create_sheet(title=title)

    def make(value: Any) -> WriteOnlyCell:
        if isinstance(value, str):
            value = _XML_ILLEGAL.sub("", value)
        cell = WriteOnlyCell(ws, value=value)
        if isinstance(value, str):
            cell.data_type = "s"  # always text: never parsed as a formula
        return cell

    ws.append([make(h) for h in head])
    for row, add in zip(rows, extra, strict=True):
        ws.append([make(v) for v in row] + [make(v) for v in add])
    workbook.save(path)
