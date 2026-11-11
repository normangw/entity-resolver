"""Abuse protection, batch, resilience, persistence and observability."""

import asyncio
import json
import logging
import threading
import time
import urllib.error
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_main import wait_ready
from test_upload import (  # noqa: F401  (client fixture)
    client,  # noqa: F401,E402  (fixture)
    csv_bytes,
    run_job,
    upload,
)

from entity_resolver import uploads
from entity_resolver.config import settings
from entity_resolver.main import app
from entity_resolver.models import ResolveResponse
from entity_resolver.observability import JsonFormatter, Metrics, metrics
from entity_resolver.protection import RateLimiter
from entity_resolver.wikidata import CircuitBreaker, WikidataClient, WikidataUnavailable


@pytest.fixture
def api(monkeypatch):
    with TestClient(app) as c:
        wait_ready(c)
        yield c


def resolved(name: str) -> ResolveResponse:
    return ResolveResponse(
        input=name,
        status="resolved",
        wikidata_id="Q1",
        canonical_name=name,
        parent_company=None,
        headquarters=None,
        confidence=1.0,
        source="local",
    )


# --- rate limiting ---------------------------------------------------------


def test_rate_limiter_refills_over_time():
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    assert [limiter.check("s", "a", 2) for _ in range(2)] == [0.0, 0.0]
    wait = limiter.check("s", "a", 2)
    assert wait == pytest.approx(30.0)  # 2 per minute: one token per 30 s
    assert limiter.check("s", "b", 2) == 0.0  # another client is unaffected
    now[0] = 31.0
    assert limiter.check("s", "a", 2) == 0.0
    assert limiter.check("s", "a", 0) == 0.0  # 0 turns the limit off


def test_resolve_is_rate_limited_per_client(api, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_resolve_per_min", 3)
    codes = [
        api.post("/resolve", json={"company_name": "Apple Inc."}).status_code
        for _ in range(5)
    ]
    assert codes == [200, 200, 200, 429, 429]
    blocked = api.post("/resolve", json={"company_name": "Apple Inc."})
    assert int(blocked.headers["retry-after"]) >= 1
    assert metrics.value("rate_limited_total", scope="resolve") >= 2


# --- API key ---------------------------------------------------------------


def test_no_api_key_configured_leaves_everything_open(api):
    assert api.get("/metrics").status_code == 200
    r = api.post("/resolve/batch", json={"company_names": ["Apple Inc."]})
    assert r.status_code == 200


def test_api_key_guards_batch_and_metrics_but_not_the_page(api, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret")
    assert api.post("/resolve/batch", json={"company_names": ["x1"]}).status_code == 401
    assert api.get("/metrics").status_code == 401
    ok = {"X-API-Key": "s3cret"}
    assert api.get("/metrics", headers=ok).status_code == 200
    assert (
        api.post(
            "/resolve/batch", json={"company_names": ["Apple"]}, headers=ok
        ).status_code
        == 200
    )
    bad = {"X-API-Key": "nope"}
    assert api.get("/metrics", headers=bad).status_code == 401
    # The single-name page keeps working unless protect_all is on.
    assert api.post("/resolve", json={"company_name": "Apple"}).status_code == 200
    assert api.get("/").status_code == 200


def test_protect_all_requires_the_key_everywhere(api, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret")
    monkeypatch.setattr(settings, "api_key_protect_all", True)
    assert api.post("/resolve", json={"company_name": "Apple"}).status_code == 401
    assert api.post("/upload", files={"file": ("a.csv", b"a\n1")}).status_code == 401
    assert api.get("/health").status_code == 200  # probes stay open
    ok = {"X-API-Key": "s3cret"}
    assert (
        api.post("/resolve", json={"company_name": "Apple"}, headers=ok).status_code
        == 200
    )


# --- body size --------------------------------------------------------------


def test_oversized_json_body_is_refused(api, monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 200)
    big = {
        "company_name": "A" * 150,
        "language": "en",
        "region": "world",
        "x": "y" * 300,
    }
    r = api.post("/resolve", json=big)
    assert r.status_code == 413
    assert api.post("/resolve", json={"company_name": "Apple"}).status_code == 200


def test_oversized_body_without_content_length_is_refused(api, monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 200)

    def chunks():
        yield b'{"company_name": "'
        yield b"A" * 500
        yield b'"}'

    r = api.post(
        "/resolve", content=chunks(), headers={"content-type": "application/json"}
    )
    assert r.status_code == 413


# --- batch ------------------------------------------------------------------


def test_batch_keeps_order_and_resolves_duplicates_once(api, monkeypatch):
    calls: list[str] = []

    async def fake(state, name, region, language="en"):
        calls.append(name)
        return resolved(name)

    monkeypatch.setattr("entity_resolver.main.resolve_name", fake)
    r = api.post(
        "/resolve/batch",
        json={"company_names": ["A Co", "B Co", " A Co ", "C Co", "B Co"]},
    )
    body = r.json()
    assert [x["input"] for x in body["results"]] == [
        "A Co",
        "B Co",
        "A Co",
        "C Co",
        "B Co",
    ]
    assert calls == ["A Co", "B Co", "C Co"]
    assert body["unique_names"] == 3 and body["retry"] == 0


def test_batch_reports_retry_per_row_when_wikidata_is_down(api, monkeypatch):
    async def fake(state, name, region, language="en"):
        if name == "Down Co":
            raise HTTPException(503, "Wikidata is unavailable; retry shortly")
        return resolved(name)

    monkeypatch.setattr("entity_resolver.main.resolve_name", fake)
    r = api.post(
        "/resolve/batch", json={"company_names": ["Up Co", "Down Co", "Up Two"]}
    )
    assert r.status_code == 200
    body = r.json()
    assert [x["status"] for x in body["results"]] == ["resolved", "retry", "resolved"]
    assert body["retry"] == 1


def test_batch_limits(api, monkeypatch):
    monkeypatch.setattr(settings, "batch_max_names", 3)
    assert (
        api.post("/resolve/batch", json={"company_names": list("abcd")}).status_code
        == 422
    )
    assert api.post("/resolve/batch", json={"company_names": []}).status_code == 422
    assert api.post("/resolve/batch", json={"company_names": ["  "]}).status_code == 422
    r = api.post("/resolve/batch", json={"company_names": ["x" * 201]})
    assert r.status_code == 422


# --- Wikidata concurrency cap, retries, breaker -------------------------------


class SlowWikidata:
    """A stand-in client whose lookups take a while."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def lookup(self, name: str, language: str = "en") -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.3)
        with self.lock:
            self.active -= 1


def test_wikidata_lookups_are_capped_and_the_overflow_gets_503(monkeypatch):
    stub = SlowWikidata()
    monkeypatch.setattr(settings, "wikidata_fallback", True)
    monkeypatch.setattr(settings, "wikidata_max_concurrent", 2)
    monkeypatch.setattr(settings, "wikidata_queue_seconds", 0.05)
    monkeypatch.setattr("entity_resolver.main.WikidataClient", lambda *a, **k: stub)
    codes: list[int] = []
    with TestClient(app) as c:
        wait_ready(c)

        def call(i: int) -> None:
            r = c.post("/resolve", json={"company_name": f"Zyxwv Quantum {i} Bakery"})
            codes.append(r.status_code)

        threads = [threading.Thread(target=call, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert stub.peak <= 2
    assert codes.count(200) == 2 and codes.count(503) == 3


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", None, None)  # type: ignore[arg-type]


def test_a_flaky_request_is_retried_with_backoff():
    attempts = []
    sleeps: list[float] = []

    def fetch(params: dict[str, str]) -> dict[str, Any]:
        attempts.append(1)
        if len(attempts) < 3:
            raise http_error(503)
        return {"search": []}

    wd = WikidataClient(fetch=fetch, retries=2, backoff=1.0, sleep=sleeps.append)
    assert wd.lookup("Acme Robotics") is None
    assert len(attempts) == 3
    assert 0.5 <= sleeps[0] <= 1.5 and 1.0 <= sleeps[1] <= 3.0  # jittered, growing


def test_throttling_is_not_retried():
    attempts = []

    def fetch(params: dict[str, str]) -> dict[str, Any]:
        attempts.append(1)
        raise http_error(429)

    wd = WikidataClient(fetch=fetch, retries=3, sleep=lambda s: None)
    with pytest.raises(WikidataUnavailable):
        wd.lookup("Acme Robotics")
    assert len(attempts) == 1


def test_circuit_breaker_opens_then_probes_after_the_cooldown():
    now = [0.0]
    attempts = []

    def fetch(params: dict[str, str]) -> dict[str, Any]:
        attempts.append(1)
        if now[0] < 100:
            raise OSError("down")
        return {"search": []}

    breaker = CircuitBreaker(threshold=2, cooldown=30, clock=lambda: now[0])
    wd = WikidataClient(fetch=fetch, breaker=breaker, cache_ttl=0)
    for _ in range(2):
        with pytest.raises(WikidataUnavailable):
            wd.lookup("Acme One")
    assert breaker.is_open and len(attempts) == 2
    with pytest.raises(WikidataUnavailable, match="circuit open"):
        wd.lookup("Acme Two")
    assert len(attempts) == 2  # not called while open
    now[0] = 200.0  # past the cooldown, and Wikidata is back
    assert wd.lookup("Acme Three") is None
    assert not breaker.is_open


# --- job persistence and re-runs ------------------------------------------------


def test_job_state_survives_a_restart(tmp_path):
    root = tmp_path / "up"
    store = uploads.JobStore(root, ttl=3600, max_running=2)
    done = store.create("a.csv", ".csv")
    done.input_path.write_text("Company\nApple\n", encoding="utf-8")
    done.state, done.total, done.done = "done", 1, 1
    done.output = done.dir / "result.csv"
    done.output.write_text("x", encoding="utf-8")
    done.cache["world\0Apple"] = resolved("Apple")
    store.save(done)
    running = store.create("b.csv", ".csv")
    running.input_path.write_text("Company\nB\n", encoding="utf-8")
    running.state = "running"
    store.save(running)

    again = uploads.JobStore(root, ttl=3600, max_running=2)
    back = again.get(done.id)
    assert back is not None and back.state == "done" and back.filename == "a.csv"
    assert back.cache["world\0Apple"].canonical_name == "Apple"
    interrupted = again.get(running.id)
    assert interrupted is not None and interrupted.state == "error"
    assert "restarted" in (interrupted.error or "")


def test_a_damaged_or_foreign_job_directory_is_ignored(tmp_path):
    root = tmp_path / "up"
    store = uploads.JobStore(root, ttl=3600, max_running=1)
    job = store.create("a.csv", ".csv")
    job.input_path.write_text("x", encoding="utf-8")
    store.save(job)
    meta = json.loads((job.dir / "meta.json").read_text())
    meta["input"] = "../../etc/passwd"  # tries to leave the job directory
    (job.dir / "meta.json").write_text(json.dumps(meta))
    assert uploads.load_job(job.dir) is None  # input resolves inside job.dir only
    (job.dir / "meta.json").write_text("{not json")
    assert uploads.load_job(job.dir) is None


def test_graceful_shutdown_saves_running_jobs(tmp_path):
    async def go() -> None:
        store = uploads.JobStore(tmp_path / "up", ttl=3600, max_running=2)
        job = store.create("a.csv", ".csv")
        job.input_path.write_text("Company\nA\n", encoding="utf-8")
        job.state = "running"
        job.task = asyncio.create_task(asyncio.sleep(60))
        await store.aclose()
        assert job.state == "error"

    asyncio.run(go())
    again = uploads.JobStore(tmp_path / "up", ttl=3600, max_running=2)
    (job,) = list(again._jobs.values())
    assert job.state == "error" and "restarted" in (job.error or "")


def test_rerun_only_looks_up_the_rows_that_were_retry(client, monkeypatch):  # noqa: F811
    calls: list[str] = []
    wikidata_up = [False]

    async def fake(state, name, region, language="en"):
        calls.append(name)
        if name == "Beta Co" and not wikidata_up[0]:
            raise HTTPException(503, "Wikidata is unavailable; retry shortly")
        return resolved(name)

    monkeypatch.setattr("entity_resolver.main.resolve_name", fake)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    rows = [["Company"], ["Alpha Co"], ["Beta Co"], ["Alpha Co"]]
    job = upload(client, csv_bytes(rows), "a.csv").json()["id"]
    first = run_job(client, job)["summary"]
    assert first["retry"] == 1 and first["partial"] is True
    wikidata_up[0] = True
    calls.clear()
    second = run_job(client, job)["summary"]
    assert calls == ["Beta Co"]  # Alpha Co was kept from the first run
    assert second["retry"] == 0 and second["partial"] is False
    assert second["resolved"] == 3


async def _no_sleep(_: float) -> None:
    return None


# --- observability ----------------------------------------------------------------


def test_health_reports_the_build(api, monkeypatch):
    monkeypatch.setattr(settings, "build_id", "abc1234")
    body = api.get("/health").json()
    assert body["build"] == "abc1234" and body["version"]


def test_request_id_is_returned_and_a_valid_incoming_one_is_kept(api):
    assert len(api.get("/health").headers["x-request-id"]) == 32
    mine = api.get("/health", headers={"X-Request-ID": "trace-12345678"})
    assert mine.headers["x-request-id"] == "trace-12345678"
    junk = api.get("/health", headers={"X-Request-ID": "bad id\twith spaces"})
    assert junk.headers["x-request-id"] != "bad id\twith spaces"


def test_metrics_count_resolutions_and_requests(api):
    before = metrics.value("resolutions_total", source="local", status="resolved")
    api.post("/resolve", json={"company_name": "Apple Inc."})
    api.post("/resolve", json={"company_name": "Zyxwv Quantum Bakery"})
    text = api.get("/metrics").text
    assert (
        metrics.value("resolutions_total", source="local", status="resolved")
        == before + 1
    )
    assert 'resolutions_total{source="none",status="unresolved"}' in text
    assert 'http_requests_total{method="POST",path="/resolve",status="200"}' in text
    assert "http_request_duration_seconds_count" in text


def test_logs_never_contain_names_or_cells(api, caplog):
    caplog.set_level(logging.DEBUG)
    api.post("/resolve", json={"company_name": "Secretive Holdings Zyxwv"})
    rows = [["Company"], ["Hushhush Industries Qwerty"]]
    job = upload(api, csv_bytes(rows), "hush.csv").json()["id"]
    run_job(api, job)
    text = " ".join(
        f"{r.getMessage()} {r.__dict__.get('path', '')}" for r in caplog.records
    )
    assert "Secretive" not in text and "Hushhush" not in text
    assert "hush.csv" not in text


def test_json_formatter_emits_one_valid_object_per_line():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("w",), None)
    record.status = 200
    line = JsonFormatter().format(record)
    data = json.loads(line)
    assert data["msg"] == "hello w" and data["status"] == 200 and "\n" not in line


def test_metrics_render_is_prometheus_text():
    m = Metrics()
    m.inc("things_total", kind="a")
    m.inc("things_total", kind="a")
    m.observe(0.02)
    out = m.render()
    assert 'things_total{kind="a"} 2' in out
    assert 'http_request_duration_seconds_bucket{le="0.025"} 1' in out
    assert 'http_request_duration_seconds_bucket{le="+Inf"} 1' in out


def test_download_name_cannot_inject_header_characters(client):  # noqa: F811
    rows = [["Company"], ["Apple Inc."]]
    meta = upload(client, csv_bytes(rows), 'evil"; x=y\\.csv').json()
    summary = run_job(client, meta["id"])["summary"]
    assert '"' not in summary["download_name"] and ";" not in summary["download_name"]
    r = client.get(f"/upload/{meta['id']}/download")
    assert r.status_code == 200
    disposition = r.headers["content-disposition"]
    assert disposition.count(";") == 1 and "evil_" in disposition


def test_unknown_http_methods_do_not_create_metric_labels(api):
    api.request("BREW", "/health")
    assert 'method="BREW"' not in api.get("/metrics").text


def test_batch_stops_calling_wikidata_when_its_time_budget_is_spent(api, monkeypatch):
    monkeypatch.setattr(settings, "batch_budget_seconds", 0.05)
    asked: list[str] = []

    async def slow(state, name, region, language="en"):
        asked.append(name)
        await asyncio.sleep(0.1)
        return resolved(name)

    monkeypatch.setattr("entity_resolver.main.resolve_name", slow)
    r = api.post(
        "/resolve/batch",
        json={"company_names": ["Aaa Co", "Bbb Co", "Ccc Co", "Apple Inc."]},
    )
    results = r.json()["results"]
    # After the first name the budget is spent: names the local index cannot settle
    # are not sent on; "Apple Inc." is, because the local index knows it.
    assert asked == ["Aaa Co", "Apple Inc."]
    assert [x["status"] for x in results] == ["resolved", "retry", "retry", "resolved"]
