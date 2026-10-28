"""End-to-end smoke tests of the spreadsheet upload flow through the real endpoints."""

import io
import time

import openpyxl
import pytest
from fastapi.testclient import TestClient

from entity_resolver import tabular
from entity_resolver.config import settings
from entity_resolver.main import app
from entity_resolver.resolver import Resolver

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Name, Notes, Country-column-that-must-survive. Includes a duplicate name, a blank
# name, an unknown company, an accented name, a non-English name and a formula.
ROWS = [
    ["Company", "Notes", "Country"],
    ["Wal-Mart Stores Inc.", "first", "US"],
    ["Apple Inc.", "second", "US"],
    ["Wal-Mart Stores Inc.", "duplicate", "US"],
    [None, "blank name", "??"],
    ["Zyxwv Quantum Bakery", "unknown", "XX"],
    ["上海航空", "chinese", "CN"],
    ['=HYPERLINK("http://evil")', "formula", "ZZ"],
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    with TestClient(app) as c:
        deadline = time.monotonic() + 5
        while c.get("/ready").status_code != 200:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        yield c


def xlsx_bytes(rows=ROWS, sheet="Data") -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    for cells in ws.iter_rows():
        for cell in cells:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"  # literal text, not a formula
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_bytes(rows=ROWS) -> bytes:
    import csv

    buf = io.StringIO(newline="")
    csv.writer(buf).writerows([["" if v is None else v for v in r] for r in rows])
    return buf.getvalue().encode("utf-8")


def upload(client, data: bytes, name: str, mime: str = "application/octet-stream"):
    return client.post("/upload", files={"file": (name, data, mime)})


def run_job(client, job_id, **body):
    body.setdefault("column", 0)
    r = client.post(f"/upload/{job_id}/resolve", json=body)
    assert r.status_code == 202, r.text
    deadline = time.monotonic() + 10
    while True:
        s = client.get(f"/upload/{job_id}/status").json()
        if s["state"] in ("done", "error"):
            return s
        assert time.monotonic() < deadline, "job did not finish"
        time.sleep(0.02)


def test_xlsx_flow_resolves_and_preserves_every_original_cell(client):
    up = upload(client, xlsx_bytes(), "companies.xlsx", XLSX)
    assert up.status_code == 200
    meta = up.json()
    assert meta["kind"] == "xlsx" and meta["sheets"] == ["Data"]

    prev = client.get(f"/upload/{meta['id']}/preview").json()
    assert prev["columns"] == ["Company", "Notes", "Country"]
    assert prev["suggested_column"] == 0
    assert len(prev["rows"]) == 6

    status = run_job(client, meta["id"], sheet="Data", column=0)
    assert status["state"] == "done" and status["done"] == status["total"] == 7
    s = status["summary"]
    assert s["passed"], [c for c in s["checks"] if not c["ok"]]
    assert s["rows"] == 7 and s["blank"] == 1
    assert s["resolved"] == 4  # Walmart x2, Apple, Shanghai Airlines
    assert s["unresolved"] == 2  # the bakery and the formula text
    assert s["unique_names"] == 5  # the duplicate and the blank do not count
    assert s["download_name"] == "companies_resolved.xlsx"

    dl = client.get(f"/upload/{meta['id']}/download")
    assert dl.status_code == 200
    ws = openpyxl.load_workbook(io.BytesIO(dl.content)).active
    out = [list(r) for r in ws.iter_rows(values_only=True)]
    assert len(out) == len(ROWS)
    assert out[0] == [*ROWS[0], *tabular.RESULT_COLUMNS]
    for original, written in zip(ROWS[1:], out[1:], strict=True):
        assert written[:3] == original  # every original cell, row for row
    by_name = {r[0]: r for r in out[1:]}
    assert by_name["Wal-Mart Stores Inc."][4] == "Walmart"
    assert by_name["Wal-Mart Stores Inc."][3] == "resolved"
    assert by_name["Zyxwv Quantum Bakery"][3] == "unresolved"
    assert out[4][3] == "blank"
    formula = ws["A8"]
    assert formula.data_type == "s"  # stored as text, so Excel never runs it


def test_csv_flow_keeps_original_rows_and_neutralizes_formulas(client):
    meta = upload(client, csv_bytes(), "list.csv", "text/csv").json()
    assert meta["kind"] == "csv" and meta["sheets"] == []
    status = run_job(client, meta["id"], column=0)
    assert status["summary"]["passed"]
    assert status["summary"]["download_name"] == "list_resolved.csv"
    dl = client.get(f"/upload/{meta['id']}/download")
    import csv

    out = list(csv.reader(io.StringIO(dl.content.decode("utf-8-sig"), newline="")))
    assert len(out) == len(ROWS)
    assert out[1][:3] == ["Wal-Mart Stores Inc.", "first", "US"]
    assert out[4][:3] == ["", "blank name", "??"]
    assert out[7][0] == '\'=HYPERLINK("http://evil")'  # neutralized, text kept


def test_region_changes_the_result(client):
    rows = [["Company"], ["Apple Inc."]]
    meta = upload(client, xlsx_bytes(rows), "a.xlsx").json()
    us = run_job(client, meta["id"], column=0, region="us")["summary"]
    cn = run_job(client, meta["id"], column=0, region="cn")["summary"]
    assert (us["resolved"], cn["resolved"]) == (1, 0)
    assert us["passed"] and cn["passed"]  # checks hold for both runs


def test_no_header_and_choosing_another_column(client):
    rows = [["note one", "Apple Inc."], ["note two", "Wal-Mart Stores Inc."]]
    meta = upload(client, csv_bytes(rows), "n.csv").json()
    prev = client.get(
        f"/upload/{meta['id']}/preview", params={"has_header": "false"}
    ).json()
    assert prev["columns"] == ["Column A", "Column B"]
    assert len(prev["rows"]) == 2  # both rows are data
    s = run_job(client, meta["id"], column=1, has_header=False)["summary"]
    assert s["rows"] == 2 and s["resolved"] == 2 and s["passed"]


def test_second_sheet_can_be_chosen(client):
    wb = openpyxl.Workbook()
    wb.active.title = "Empty"
    wb.active.append(["x"])
    wb.create_sheet("Real").append(["Company"])
    wb["Real"].append(["Apple Inc."])
    buf = io.BytesIO()
    wb.save(buf)
    meta = upload(client, buf.getvalue(), "two.xlsx").json()
    assert meta["sheets"] == ["Empty", "Real"]
    s = run_job(client, meta["id"], sheet="Real", column=0)["summary"]
    assert s["rows"] == 1 and s["resolved"] == 1
    r = client.post(
        f"/upload/{meta['id']}/resolve", json={"sheet": "Nope", "column": 0}
    )
    assert r.status_code == 400


def test_wrong_column_and_empty_sheet_are_rejected(client):
    meta = upload(client, csv_bytes([["Company"], ["Apple Inc."]]), "a.csv").json()
    r = client.post(f"/upload/{meta['id']}/resolve", json={"column": 5})
    assert r.status_code == 400
    meta = upload(client, csv_bytes([["Company"]]), "b.csv").json()
    r = client.post(f"/upload/{meta['id']}/resolve", json={"column": 0})
    assert r.status_code == 400
    assert "no data rows" in r.json()["detail"]


def test_too_many_rows_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "upload_max_rows", 3)
    rows = [["Company"]] + [["Apple Inc."]] * 4
    meta = upload(client, csv_bytes(rows), "big.csv").json()
    r = client.post(f"/upload/{meta['id']}/resolve", json={"column": 0})
    assert r.status_code == 400 and "more than 3" in r.json()["detail"]
    rows = [["Company"]] + [["Apple Inc."]] * 3
    meta = upload(client, csv_bytes(rows), "ok.csv").json()
    assert run_job(client, meta["id"])["summary"]["rows"] == 3


def test_overlong_name_is_skipped_not_dropped(client):
    rows = [["Company"], ["A" * 300], ["Apple Inc."]]
    meta = upload(client, csv_bytes(rows), "long.csv").json()
    s = run_job(client, meta["id"])["summary"]
    assert s["skipped"] == 1 and s["resolved"] == 1 and s["rows"] == 2 and s["passed"]


def test_upload_rejections(client):
    assert upload(client, b"a,b\n", "x.txt").status_code == 400
    assert upload(client, b"x", "macro.xlsm").status_code == 400
    assert upload(client, b"MZ\x90\x00", "fake.xlsx").status_code == 400
    assert upload(client, b"PK\x03\x04junk", "bad.xlsx").status_code == 400
    assert upload(client, xlsx_bytes(), "renamed.csv").status_code == 400
    assert (
        upload(client, b"", "empty.csv").status_code == 200
    )  # type is fine; no rows later


def test_oversized_upload_is_refused_and_cleaned_up(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "upload_max_bytes", 100)
    r = upload(client, b"a" * 500, "big.csv")
    assert r.status_code == 413
    assert list((tmp_path / "uploads").iterdir()) == []


def test_unknown_or_malformed_ids_are_404(client):
    for bad in ("0" * 32, "../etc", "not-an-id"):
        assert client.get(f"/upload/{bad}/status").status_code == 404
        assert client.get(f"/upload/{bad}/download").status_code == 404


def test_download_before_done_is_409(client):
    meta = upload(client, csv_bytes([["Company"], ["Apple Inc."]]), "a.csv").json()
    assert client.get(f"/upload/{meta['id']}/download").status_code == 409


def test_resolve_while_model_loading_is_503(client):
    meta = upload(client, csv_bytes([["Company"], ["Apple Inc."]]), "a.csv").json()
    saved, app.state.resolver = app.state.resolver, None
    try:
        r = client.post(f"/upload/{meta['id']}/resolve", json={"column": 0})
    finally:
        app.state.resolver = saved
    assert r.status_code == 503


def test_expired_uploads_are_swept(client, monkeypatch, tmp_path):
    meta = upload(client, csv_bytes([["Company"], ["Apple Inc."]]), "a.csv").json()
    monkeypatch.setattr(settings, "upload_ttl_seconds", 1)
    app.state.jobs.ttl = 0
    upload(client, csv_bytes([["Company"], ["x"]]), "b.csv")  # triggers a sweep
    assert client.get(f"/upload/{meta['id']}/status").status_code == 404
    assert not (tmp_path / "uploads" / meta["id"]).exists()


def test_sweep_leaves_foreign_directories_alone(tmp_path):
    from entity_resolver.uploads import JobStore

    root = tmp_path / "shared"
    (root / "somebody-elses-data").mkdir(parents=True)
    store = JobStore(root, ttl=0, max_running=1)
    store.sweep()
    assert (root / "somebody-elses-data").exists()


def test_verify_catches_a_dropped_or_altered_row(tmp_path):
    """The integrity checks fail loudly if the output does not match the input."""
    from entity_resolver import uploads

    data = [["Apple Inc.", "keep"], ["Walmart", "keep"]]
    table = uploads.Table(["Company", "Note"], data, 0, "S")
    extra = [["resolved", "Apple", "Q312", None, None, "US", 0.9, "local"]] * 2
    good = tmp_path / "good.csv"
    tabular.write_result(good, "csv", "S", table.labels, data, extra)
    assert all(
        c.ok for c in uploads.verify_output(good, "csv", table, ["resolved"] * 2)
    )

    dropped = tmp_path / "dropped.csv"
    tabular.write_result(dropped, "csv", "S", table.labels, data[:1], extra[:1])
    failed = {
        c.name
        for c in uploads.verify_output(dropped, "csv", table, ["resolved"] * 2)
        if not c.ok
    }
    assert {"Row count", "No company name lost"} <= failed

    altered = tmp_path / "altered.csv"
    tabular.write_result(
        altered, "csv", "S", table.labels, [["Apple Inc.", "CHANGED"], data[1]], extra
    )
    failed = {
        c.name
        for c in uploads.verify_output(altered, "csv", table, ["resolved"] * 2)
        if not c.ok
    }
    assert failed == {"Original cells unchanged"}


def test_resolver_failure_marks_job_error_without_leaking(client, monkeypatch):
    def boom(self, name, region="world"):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(Resolver, "resolve", boom)
    meta = upload(client, csv_bytes([["Company"], ["Apple Inc."]]), "a.csv").json()
    s = run_job(client, meta["id"])
    assert s["state"] == "error"
    assert "secret" not in str(s)
