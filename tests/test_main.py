import asyncio
import threading
import time

import httpx
from fastapi.testclient import TestClient

from entity_resolver.main import app
from entity_resolver.resolver import Entity, Match, Resolver


def wait_ready(client: TestClient, timeout: float = 5.0) -> None:
    """Block until the background model load has finished."""
    deadline = time.monotonic() + timeout
    while client.get("/ready").status_code != 200:
        assert time.monotonic() < deadline, "model did not finish loading"
        time.sleep(0.01)


def test_health_returns_ok():
    """Health endpoint should return status ok and a version string."""
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


def test_ready_after_startup():
    """/ready returns 200 once the lifespan has loaded the resolver."""
    with TestClient(app) as client:
        wait_ready(client)
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_not_ready_before_startup():
    """/ready returns 503 while the resolver has not been loaded."""
    # No `with`: the lifespan does not run, so app.state has no resolver.
    client = TestClient(app)
    had_resolver = hasattr(app.state, "resolver")
    saved = getattr(app.state, "resolver", None)
    if had_resolver:
        del app.state.resolver
    try:
        response = client.get("/ready")
    finally:
        if had_resolver:
            app.state.resolver = saved
    assert response.status_code == 503
    assert response.json() == {"status": "not ready"}


def test_settings_rejects_bad_timeout():
    """Settings should reject a non-integer wikidata_timeout."""
    from pydantic import ValidationError

    from entity_resolver.config import Settings

    try:
        Settings(wikidata_timeout="not-a-number")
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_resolve_known_company():
    with TestClient(app) as client:
        wait_ready(client)
        r = client.post("/resolve", json={"company_name": "Wal-Mart Stores Inc."})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "resolved"
    assert data["canonical_name"] == "Walmart"
    assert data["wikidata_id"] == "Q483551"


def test_resolve_non_english_alias():
    with TestClient(app) as client:
        wait_ready(client)
        r = client.post("/resolve", json={"company_name": "上海航空", "language": "zh"})
    assert r.json()["canonical_name"] == "Shanghai Airlines"


def test_resolve_unknown_company_is_unresolved():
    with TestClient(app) as client:
        wait_ready(client)
        r = client.post("/resolve", json={"company_name": "Zyxwv Quantum Bakery"})
    assert r.json()["status"] == "unresolved"


def test_resolve_rejects_blank_name():
    with TestClient(app) as client:
        r = client.post("/resolve", json={"company_name": "   "})
    assert r.status_code == 422


def test_resolve_uses_stubbed_model(monkeypatch):
    """The endpoint returns the model client's result without the real index."""

    def fake_resolve(self: Resolver, name: str, region: str = "world") -> Match:
        return Match(
            entity=Entity(
                wikidata_id="QSTUB",
                canonical_name="Stub Company",
                parent_company="Stub Parent",
                headquarters="Stub City",
            ),
            score=0.42,
        )

    monkeypatch.setattr(Resolver, "resolve", fake_resolve)
    with TestClient(app) as client:
        wait_ready(client)
        response = client.post("/resolve", json={"company_name": "not in the index"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "resolved"
    assert data["wikidata_id"] == "QSTUB"
    assert data["canonical_name"] == "Stub Company"
    assert data["parent_company"] == "Stub Parent"
    assert data["headquarters"] == "Stub City"
    assert data["confidence"] == 0.42


def test_health_answers_during_slow_resolve(monkeypatch):
    """/health returns while a slow /resolve is still in flight."""
    started = threading.Event()

    def slow_resolve(self: Resolver, name: str, region: str = "world") -> None:
        started.set()
        time.sleep(1.0)
        return None

    monkeypatch.setattr(Resolver, "resolve", slow_resolve)

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            pending = asyncio.create_task(
                client.post("/resolve", json={"company_name": "Walmart"})
            )
            assert await asyncio.to_thread(started.wait, 5)
            t0 = time.monotonic()
            health = await client.get("/health")
            elapsed = time.monotonic() - t0
            assert health.status_code == 200
            assert health.json()["status"] == "ok"
            assert elapsed < 0.5
            done = await pending
            assert done.status_code == 200

    asyncio.run(run())


def test_requests_during_loading(monkeypatch):
    """/health is 200 while loading; /ready and /resolve are 503 until done."""
    release = threading.Event()
    real_init = Resolver.__init__

    def slow_init(self: Resolver, *args, **kwargs) -> None:
        assert release.wait(5)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(Resolver, "__init__", slow_init)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        r = client.post("/resolve", json={"company_name": "Walmart"})
        assert r.status_code == 503
        release.set()
        wait_ready(client)
        r = client.post("/resolve", json={"company_name": "Walmart"})
        assert r.status_code == 200
        assert r.json()["status"] == "resolved"


def test_resolve_region_filters_results():
    """A region the company is not in comes back unresolved."""
    with TestClient(app) as client:
        wait_ready(client)
        inside = client.post(
            "/resolve", json={"company_name": "Apple Inc.", "region": "us"}
        ).json()
        outside = client.post(
            "/resolve", json={"company_name": "Apple Inc.", "region": "cn"}
        ).json()
    assert inside["status"] == "resolved"
    assert inside["country"] == "US"
    assert outside["status"] == "unresolved"


def test_resolve_rejects_unknown_region():
    with TestClient(app) as client:
        r = client.post("/resolve", json={"company_name": "Apple", "region": "mars"})
    assert r.status_code == 422


def _walmart_candidate() -> Match:
    return Match(
        entity=Entity(
            wikidata_id="Q483551",
            canonical_name="Walmart",
            parent_company=None,
            headquarters="Bentonville, Arkansas, United States",
            country="US",
            identifier="Y87794H0US1R65VBXU25",
        ),
        score=0.65,
    )


def _force_low_confidence(monkeypatch) -> None:
    monkeypatch.setattr(Resolver, "resolve", lambda self, name, region="world": None)
    monkeypatch.setattr(
        Resolver,
        "candidates",
        lambda self, name, region="world", min_score=0.0, limit=3: [
            _walmart_candidate()
        ],
    )


_WALMART_RECORD = {
    "data": [
        {
            "id": "Y87794H0US1R65VBXU25",
            "attributes": {
                "entity": {
                    "legalName": {"name": "WALMART INC."},
                    "status": "ACTIVE",
                }
            },
        }
    ]
}


def _stub_gleif(monkeypatch, payload=_WALMART_RECORD) -> None:
    from entity_resolver import gleif

    monkeypatch.setattr(
        gleif.HttpGleifClient,
        "_fetch",
        lambda self, leis: gleif.parse_response(payload),
    )


def test_low_confidence_is_settled_by_gleif(monkeypatch):
    """With the tiebreak on, a candidate whose legal name fits is accepted."""
    from entity_resolver.config import settings

    _force_low_confidence(monkeypatch)
    _stub_gleif(monkeypatch)
    monkeypatch.setattr(settings, "gleif_enabled", True)
    with TestClient(app) as client:
        wait_ready(client)
        data = client.post("/resolve", json={"company_name": "Walmart Inc"}).json()
    assert data["status"] == "resolved"
    assert data["source"] == "gleif"
    assert "Bentonville" in data["headquarters"]  # still Wikidata's, not GLEIF's


def test_low_confidence_stays_unresolved_when_gleif_is_off(monkeypatch):
    from entity_resolver.config import settings

    _force_low_confidence(monkeypatch)
    monkeypatch.setattr(settings, "gleif_enabled", False)
    with TestClient(app) as client:
        wait_ready(client)
        data = client.post("/resolve", json={"company_name": "Walmart Inc"}).json()
    assert data["status"] == "unresolved"
    assert data["source"] is None


def test_gleif_failure_falls_back_to_unresolved(monkeypatch):
    from entity_resolver import gleif
    from entity_resolver.config import settings

    def boom(self, leis):
        raise OSError("gleif down")

    _force_low_confidence(monkeypatch)
    monkeypatch.setattr(settings, "gleif_enabled", True)
    monkeypatch.setattr(gleif.HttpGleifClient, "_fetch", boom)
    with TestClient(app) as client:
        wait_ready(client)
        r = client.post("/resolve", json={"company_name": "Walmart Inc"})
    assert r.status_code == 200
    assert r.json()["status"] == "unresolved"


def test_local_match_reports_local_source():
    with TestClient(app) as client:
        wait_ready(client)
        data = client.post("/resolve", json={"company_name": "Apple Inc."}).json()
    assert data["source"] == "local"


def test_index_serves_web_ui():
    """GET / returns the HTML page that calls POST /resolve."""
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert 'fetch("/resolve"' in body
    assert "上海航空" in body  # a non-English example input
    assert "Unresolved" in body
    assert 'fetch("/upload"' in body  # the spreadsheet upload panel


def test_index_not_in_openapi_schema():
    """The UI route is hidden from /docs so the documented API stays clean."""
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]
    assert "/" not in paths
    assert "/resolve" in paths
