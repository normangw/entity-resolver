from fastapi.testclient import TestClient

from entity_resolver.main import app


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
