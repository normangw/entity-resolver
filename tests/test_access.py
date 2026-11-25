"""Who may see what: upload ownership, the API key, and Cache-Control (#100)."""

import time

import pytest
from fastapi.testclient import TestClient
from test_upload import csv_bytes

from entity_resolver.config import settings
from entity_resolver.main import OWNER_COOKIE, app

ROWS = [["Company"], ["Apple Inc."], ["Wal-Mart Stores Inc."]]


def _ready(client: TestClient) -> TestClient:
    """Wait until ``client``'s app has loaded the index, then return it."""
    deadline = time.monotonic() + 5
    while client.get("/ready").status_code != 200:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return client


@pytest.fixture
def two_browsers(tmp_path, monkeypatch):
    """Two clients over HTTPS with separate cookie jars, like two browsers."""
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    base = "https://testserver"
    with TestClient(app, base_url=base) as a, TestClient(app, base_url=base) as b:
        yield _ready(a), _ready(b)


def _upload(client: TestClient) -> str:
    """Upload the small CSV as ``client`` and return the job id."""
    r = client.post("/upload", files={"file": ("list.csv", csv_bytes(ROWS))})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _finish(client: TestClient, job_id: str) -> dict:
    """Start the job as ``client`` and wait until it is done."""
    r = client.post(f"/upload/{job_id}/resolve", json={"column": 0})
    assert r.status_code == 202, r.text
    deadline = time.monotonic() + 10
    while True:
        s = client.get(f"/upload/{job_id}/status").json()
        if s["state"] in ("done", "error"):
            return s
        assert time.monotonic() < deadline
        time.sleep(0.02)


# --- upload ownership -------------------------------------------------------


def test_owner_cookie_is_httponly_strict_secure_and_scoped(two_browsers):
    """Over HTTPS the cookie is HttpOnly, SameSite=Strict, Secure, Path=/upload."""
    a, _ = two_browsers
    r = a.post("/upload", files={"file": ("list.csv", csv_bytes(ROWS))})
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith(OWNER_COOKIE + "=")
    for part in ("httponly", "samesite=strict", "secure", "path=/upload"):
        assert part in cookie


def test_cookie_is_not_secure_over_plain_http(tmp_path, monkeypatch):
    """A plain-HTTP local run gets a cookie the browser will send back."""
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    with TestClient(app) as c:
        r = _ready(c).post("/upload", files={"file": ("l.csv", csv_bytes(ROWS))})
    assert "secure" not in r.headers["set-cookie"].lower()


def test_owner_can_use_every_job_route(two_browsers):
    """The uploader can preview, resolve, poll and download."""
    a, _ = two_browsers
    job = _upload(a)
    assert a.get(f"/upload/{job}/preview").status_code == 200
    assert _finish(a, job)["state"] == "done"
    assert a.get(f"/upload/{job}/download").status_code == 200


def test_another_browser_gets_404_on_every_job_route(two_browsers):
    """A second browser cannot preview, start, poll or download the first's job."""
    a, b = two_browsers
    job = _upload(a)
    assert b.get(f"/upload/{job}/preview").status_code == 404
    assert b.post(f"/upload/{job}/resolve", json={"column": 0}).status_code == 404
    _finish(a, job)
    assert b.get(f"/upload/{job}/status").status_code == 404
    r = b.get(f"/upload/{job}/download")
    assert r.status_code == 404
    # The same answer as an id that does not exist, so it confirms nothing.
    assert r.json() == b.get("/upload/" + "0" * 32 + "/status").json()


def test_no_cookie_and_a_forged_cookie_both_get_404(two_browsers):
    """No cookie, or a well-formed but wrong one, gets a 404."""
    a, b = two_browsers
    job = _upload(a)
    b.cookies.clear()
    assert b.get(f"/upload/{job}/preview").status_code == 404
    b.cookies.set(OWNER_COOKIE, "x" * 43, domain="testserver.local", path="/upload")
    assert b.get(f"/upload/{job}/preview").status_code == 404


def test_one_browser_keeps_access_to_all_its_uploads(two_browsers):
    """A second upload reuses the token, so the first job stays reachable."""
    a, _ = two_browsers
    first, second = _upload(a), _upload(a)
    assert a.get(f"/upload/{first}/preview").status_code == 200
    assert a.get(f"/upload/{second}/preview").status_code == 200


def test_only_the_token_hash_is_stored(two_browsers):
    """The job keeps the token's SHA-256, never the token."""
    a, _ = two_browsers
    job_id = _upload(a)
    token = a.cookies.get(OWNER_COOKIE)
    job = app.state.jobs.get(job_id)
    assert token and job.owner and token not in job.owner
    assert len(job.owner) == 64  # sha256 hex


def test_owner_survives_a_restart_and_old_jobs_stay_closed(two_browsers):
    """The owner hash is saved with the job; a job with no owner opens for no one."""
    from entity_resolver import uploads

    a, _ = two_browsers
    job_id = _upload(a)
    _finish(a, job_id)
    job = app.state.jobs.get(job_id)
    restored = uploads.load_job(job.dir)
    assert restored is not None and restored.owner == job.owner
    # A job saved by a version without ownership has no owner: no one can open it.
    job.owner = None
    assert a.get(f"/upload/{job_id}/status").status_code == 404


# --- API key -----------------------------------------------------------------


@pytest.fixture
def keyed(monkeypatch):
    """A client against the app with API_KEY set."""
    monkeypatch.setattr(settings, "api_key", "s3cret")
    with TestClient(app) as c:
        yield _ready(c)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/metrics", None),
        ("POST", "/resolve/batch", {"company_names": ["Apple Inc."]}),
    ],
)
def test_protected_routes_answer_401_without_or_with_a_wrong_key(
    keyed, method, path, body
):
    """No key, a wrong key, or the wrong auth scheme is a 401 with a message."""
    for headers in (
        {},
        {"X-API-Key": "wrong"},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic s3cret"},
        {"Authorization": "s3cret"},
    ):
        r = keyed.request(method, path, json=body, headers=headers)
        assert r.status_code == 401, headers
        assert "API key" in r.json()["detail"]
        assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "headers",
    [
        {"X-API-Key": "s3cret"},
        {"Authorization": "Bearer s3cret"},
        {"Authorization": "bearer s3cret"},
    ],
)
def test_either_header_form_is_accepted(keyed, headers):
    """``X-API-Key`` and ``Authorization: Bearer`` both open the routes."""
    assert keyed.get("/metrics", headers=headers).status_code == 200
    r = keyed.post(
        "/resolve/batch", json={"company_names": ["Apple Inc."]}, headers=headers
    )
    assert r.status_code == 200


def test_a_wrong_x_api_key_is_not_rescued_by_a_bearer_token(keyed):
    """When ``X-API-Key`` is sent it is the one checked."""
    headers = {"X-API-Key": "wrong", "Authorization": "Bearer s3cret"}
    assert keyed.get("/metrics", headers=headers).status_code == 401


# --- Cache-Control -------------------------------------------------------------


def test_page_is_no_cache_and_answers_are_no_store(two_browsers):
    """``/`` is no-cache; JSON answers, errors and docs are no-store."""
    a, _ = two_browsers
    assert a.get("/").headers["cache-control"] == "no-cache"
    for r in (
        a.get("/health"),
        a.get("/ready"),
        a.post("/resolve", json={"company_name": "Apple Inc."}),
        a.post("/resolve", json={}),  # a 422 is per-request too
        a.get("/openapi.json"),
    ):
        assert r.headers["cache-control"] == "no-store", r.url


def test_upload_status_and_download_are_no_store(two_browsers):
    """A job's status and its result file are never cached."""
    a, _ = two_browsers
    job = _upload(a)
    _finish(a, job)
    assert a.get(f"/upload/{job}/status").headers["cache-control"] == "no-store"
    assert a.get(f"/upload/{job}/download").headers["cache-control"] == "no-store"


def test_body_limit_413_is_no_store(two_browsers, monkeypatch):
    """The body-size middleware's own 413 gets the header too."""
    a, _ = two_browsers
    monkeypatch.setattr(settings, "max_body_bytes", 10)
    r = a.post("/resolve", json={"company_name": "A" * 50})
    assert r.status_code == 413
    assert r.headers["cache-control"] == "no-store"
