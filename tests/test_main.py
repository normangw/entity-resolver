from fastapi.testclient import TestClient

from entity_resolver.main import app

client = TestClient(app)


def test_health_returns_ok():
    """Health endpoint should return status ok and a version string."""
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
