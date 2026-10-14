import asyncio
import threading
import time

import httpx
from fastapi.testclient import TestClient

from entity_resolver.main import app
from entity_resolver.resolver import Entity, Match, Resolver


def test_health_returns_ok():
    """Health endpoint should return status ok and a version string."""
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


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
        r = client.post("/resolve", json={"company_name": "Wal-Mart Stores Inc."})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "resolved"
    assert data["canonical_name"] == "Walmart"
    assert data["wikidata_id"] == "Q483551"


def test_resolve_non_english_alias():
    with TestClient(app) as client:
        r = client.post("/resolve", json={"company_name": "上海航空", "language": "zh"})
    assert r.json()["canonical_name"] == "Shanghai Airlines"


def test_resolve_unknown_company_is_unresolved():
    with TestClient(app) as client:
        r = client.post("/resolve", json={"company_name": "Zyxwv Quantum Bakery"})
    assert r.json()["status"] == "unresolved"


def test_resolve_rejects_blank_name():
    with TestClient(app) as client:
        r = client.post("/resolve", json={"company_name": "   "})
    assert r.status_code == 422


def test_resolve_uses_stubbed_model(monkeypatch):
    """The endpoint returns the model client's result without the real index."""

    def fake_resolve(self: Resolver, name: str) -> Match:
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

    def slow_resolve(self: Resolver, name: str) -> None:
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
