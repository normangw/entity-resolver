"""429 after Retry-After, the lookup deadline, Retry-After on every 503, repeated
writes, the scorer swap, and job files across versions (#101)."""

import json
import threading
import time
import urllib.error
from email.message import Message
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_main import wait_ready
from test_upload import csv_bytes

from entity_resolver import uploads
from entity_resolver.config import settings
from entity_resolver.main import SCORERS, app
from entity_resolver.wikidata import CircuitBreaker, WikidataClient, WikidataUnavailable

EMPTY = {"search": []}


def throttled(retry_after: str | None) -> urllib.error.HTTPError:
    """A 429 from Wikidata, with ``retry_after`` as its header when given."""
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://x", 429, "Too Many Requests", headers, None)


class Script:
    """A fake fetch that raises or returns the given outcomes, in order."""

    def __init__(self, *outcomes: Any) -> None:
        """Queue ``outcomes``; the last one repeats once the queue is empty."""
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, params: dict[str, str]) -> dict[str, Any]:
        """Serve the next outcome."""
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# --- 429 after Retry-After ----------------------------------------------------


def test_short_retry_after_is_waited_out_once_then_succeeds():
    """429 with Retry-After: 1 -> one wait of at least 1 s, then the answer."""
    sleeps: list[float] = []
    fetch = Script(throttled("1"), EMPTY)
    wd = WikidataClient(fetch=fetch, sleep=sleeps.append)
    assert wd.lookup("Acme Robotics") is None
    assert fetch.calls == 2
    assert len(sleeps) == 1 and 1.0 <= sleeps[0] <= 1.25


def test_long_retry_after_becomes_a_503_with_the_same_retry_after():
    """429 with Retry-After: 30 is not waited; the error carries the 30 s."""
    sleeps: list[float] = []
    fetch = Script(throttled("30"))
    wd = WikidataClient(fetch=fetch, sleep=sleeps.append)
    with pytest.raises(WikidataUnavailable) as caught:
        wd.lookup("Acme Robotics")
    assert caught.value.retry_after == 30
    assert fetch.calls == 1 and sleeps == []


def test_only_one_429_wait_per_lookup():
    """A second 429 in the same lookup ends it, even with a short Retry-After."""
    sleeps: list[float] = []
    fetch = Script(throttled("1"), throttled("1"), EMPTY)
    wd = WikidataClient(fetch=fetch, sleep=sleeps.append)
    with pytest.raises(WikidataUnavailable):
        wd.lookup("Acme Robotics")
    assert fetch.calls == 2 and len(sleeps) == 1


def test_an_http_date_retry_after_is_understood():
    """Retry-After may be an HTTP date; a past date means retry now."""
    sleeps: list[float] = []
    fetch = Script(throttled("Wed, 21 Oct 2015 07:28:00 GMT"), EMPTY)
    assert WikidataClient(fetch=fetch, sleep=sleeps.append).lookup("Acme") is None
    assert sleeps == [0.0]


def test_a_429_and_its_retry_count_once_toward_the_breaker():
    """Repeated 429s open the breaker after N lookups, not 2N."""
    fetch = Script(throttled("1"))
    breaker = CircuitBreaker(threshold=3, cooldown=60)
    wd = WikidataClient(fetch=fetch, sleep=lambda s: None, breaker=breaker)
    for n in range(3):
        assert not breaker.is_open, n
        with pytest.raises(WikidataUnavailable):
            wd.lookup(f"Acme {n}")
    assert breaker.is_open
    assert fetch.calls == 6  # 3 lookups, each a 429 and its one retry


def test_open_breaker_error_says_when_to_come_back():
    """While open, the error's retry_after is the breaker's remaining cooldown."""
    now = [0.0]
    breaker = CircuitBreaker(threshold=1, cooldown=30, clock=lambda: now[0])
    wd = WikidataClient(fetch=Script(OSError("down")), breaker=breaker)
    with pytest.raises(WikidataUnavailable):
        wd.lookup("Acme One")
    now[0] = 10.0
    with pytest.raises(WikidataUnavailable, match="circuit open") as caught:
        wd.lookup("Acme Two")
    assert caught.value.retry_after == pytest.approx(20.0)


# --- the overall deadline -------------------------------------------------------


def test_no_call_starts_after_the_deadline():
    """Slow calls stop at the deadline instead of running every retry."""
    now = [0.0]

    def slow(params: dict[str, str]) -> dict[str, Any]:
        """Each call takes 4 s and then times out."""
        now[0] += 4.0
        raise TimeoutError("timed out")

    wd = WikidataClient(
        fetch=slow, clock=lambda: now[0], retries=5, backoff=0.1, deadline=10.0
    )
    wd._sleep = lambda s: now.__setitem__(0, now[0] + s)
    with pytest.raises(WikidataUnavailable):
        wd.lookup("Acme Robotics")
    assert now[0] <= 10.0 + 4.0  # the deadline plus at most one call


def test_a_429_wait_that_would_pass_the_deadline_is_not_taken():
    """A short Retry-After still is not waited when the deadline is closer."""
    now = [0.0]
    sleeps: list[float] = []

    def fetch(params: dict[str, str]) -> dict[str, Any]:
        """The first call takes 9.5 s and gets a 429 with Retry-After: 2."""
        now[0] += 9.5
        raise throttled("2")

    wd = WikidataClient(
        fetch=fetch, clock=lambda: now[0], deadline=10.0, sleep=sleeps.append
    )
    with pytest.raises(WikidataUnavailable) as caught:
        wd.lookup("Acme Robotics")
    assert sleeps == [] and caught.value.retry_after == 2


# --- Retry-After on every 503 ------------------------------------------------------


class Unavailable:
    """A Wikidata stand-in that is always unavailable."""

    def __init__(self, retry_after: float) -> None:
        """Fail every lookup with ``retry_after``."""
        self.retry_after = retry_after

    def lookup(self, name: str, language: str = "en") -> None:
        """Raise WikidataUnavailable."""
        raise WikidataUnavailable("down", retry_after=self.retry_after)


def test_wikidata_503_passes_retry_after_on():
    """Our 503 tells the caller when to come back, rounded up to whole seconds."""
    with TestClient(app) as client:
        wait_ready(client)
        app.state.wikidata = Unavailable(29.2)
        try:
            r = client.post("/resolve", json={"company_name": "Zyxwv Robotics"})
        finally:
            app.state.wikidata = None
    assert r.status_code == 503
    assert r.headers["retry-after"] == "30"


def test_loading_503s_carry_retry_after():
    """/ready, /resolve and /resolve/batch say when to retry while loading."""
    with TestClient(app) as client:
        wait_ready(client)
        saved, app.state.resolver = app.state.resolver, None
        try:
            responses = [
                client.get("/ready"),
                client.post("/resolve", json={"company_name": "Apple"}),
                client.post("/resolve/batch", json={"company_names": ["Apple"]}),
            ]
        finally:
            app.state.resolver = saved
    for r in responses:
        assert r.status_code == 503 and int(r.headers["retry-after"]) >= 1, r.url


# --- repeated writes --------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A ready client with its own upload directory."""
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    with TestClient(app) as c:
        wait_ready(c)
        yield c


def test_two_starts_at_once_give_one_202_and_one_409(client, monkeypatch):
    """The job is claimed before the sheet is read, so a racing start gets 409."""
    job = client.post(
        "/upload", files={"file": ("a.csv", csv_bytes([["Company"], ["Apple"]]))}
    ).json()["id"]
    real_load = uploads.load_table
    second: list[int] = []

    def slow_load(*args, **kwargs):
        """While the first start reads the sheet, send the second start."""
        second.append(
            client.post(f"/upload/{job}/resolve", json={"column": 0}).status_code
        )
        return real_load(*args, **kwargs)

    monkeypatch.setattr(uploads, "load_table", slow_load)
    first = client.post(f"/upload/{job}/resolve", json={"column": 0})
    assert first.status_code == 202
    assert second == [409]


def test_a_failed_start_releases_the_job(client):
    """A bad column is a 400 and the job can be started again."""
    job = client.post(
        "/upload", files={"file": ("a.csv", csv_bytes([["Company"], ["Apple"]]))}
    ).json()["id"]
    assert client.post(f"/upload/{job}/resolve", json={"column": 9}).status_code == 400
    assert client.get(f"/upload/{job}/status").json()["state"] == "uploaded"
    assert client.post(f"/upload/{job}/resolve", json={"column": 0}).status_code == 202


# --- swapping the scorer ------------------------------------------------------------


def test_scorer_setting_picks_the_rapidfuzz_function(monkeypatch):
    """SCORER in .env chooses the function the resolver is built with."""
    from entity_resolver import main

    seen: list[Any] = []
    monkeypatch.setattr(settings, "scorer", "token_set_ratio")
    monkeypatch.setattr(
        main, "Resolver", lambda *a, **kw: seen.append(kw["scorer"]) or object()
    )
    main._load_resolver()
    assert seen == [SCORERS["token_set_ratio"]]


# --- job files across versions ------------------------------------------------------


def _job_dir(tmp_path, meta: dict) -> Any:
    """A job directory holding ``meta`` as its meta.json and an input file."""
    job_dir = tmp_path / ("a" * 32)
    job_dir.mkdir()
    (job_dir / "input.csv").write_text("Company\nApple\n", encoding="utf-8")
    (job_dir / uploads.META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    return job_dir


def test_load_job_ignores_unknown_fields(tmp_path):
    """A newer version's extra fields do not stop an older one reading the job."""
    meta = {
        "id": "a" * 32,
        "input": "input.csv",
        "filename": "a.csv",
        "kind": "csv",
        "created": time.time(),
        "state": "uploaded",
        "done": 0,
        "total": 0,
        "error": None,
        "output": None,
        "summary": None,
        "owner": "f" * 64,
        "added_in_a_later_version": {"x": 1},
    }
    job = uploads.load_job(_job_dir(tmp_path, meta))
    assert job is not None and job.owner == "f" * 64


def test_load_job_fills_in_missing_fields(tmp_path):
    """An older version's file without owner, summary or counts still loads."""
    job = uploads.load_job(_job_dir(tmp_path, {"id": "a" * 32, "input": "input.csv"}))
    assert job is not None
    assert (job.state, job.done, job.total, job.owner) == ("uploaded", 0, 0, None)


def test_load_job_drops_answers_and_summaries_it_cannot_read(tmp_path):
    """A cached answer in a new shape is redone; a bad summary means resolve again."""
    meta = {
        "id": "a" * 32,
        "input": "input.csv",
        "state": "done",
        "output": "input.csv",
        "summary": {"not": "a summary"},
    }
    job_dir = _job_dir(tmp_path, meta)
    (job_dir / uploads.CACHE_FILE).write_text(
        json.dumps({"world\0Apple": {"shape": "new"}}), encoding="utf-8"
    )
    job = uploads.load_job(job_dir)
    assert job is not None
    assert job.cache == {} and job.summary is None
    assert job.state == "error" and "Resolve it again" in (job.error or "")


def test_load_job_turns_an_unknown_state_into_an_error(tmp_path):
    """A state only a newer version knows becomes an error that says what to do."""
    meta = {"id": "a" * 32, "input": "input.csv", "state": "queued"}
    job = uploads.load_job(_job_dir(tmp_path, meta))
    assert job is not None and job.state == "error"


def test_every_fetch_on_the_page_has_a_time_limit():
    """The page calls fetch only inside call(), which aborts after LIMIT_MS."""
    from pathlib import Path

    page = Path(__file__).resolve().parents[1] / "src/entity_resolver/static/index.html"
    html = page.read_text(encoding="utf-8")
    assert html.count("fetch(") == 1
    assert "signal: ctl.signal" in html and "LIMIT_MS" in html


def test_lookups_on_two_threads_keep_separate_deadlines():
    """The deadline and the 429 wait are per lookup, per thread."""
    sleeps: list[float] = []
    wd = WikidataClient(fetch=Script(throttled("1"), EMPTY), sleep=sleeps.append)
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(wd.lookup("Acme A"))),
        threading.Thread(target=lambda: results.append(wd.lookup("Acme B"))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [None, None]
