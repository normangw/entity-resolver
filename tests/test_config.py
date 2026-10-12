import pytest
from pydantic import ValidationError

from entity_resolver.config import Settings


@pytest.mark.parametrize("timeout", [0, -1])
def test_rejects_nonpositive_timeout(timeout: int) -> None:
    with pytest.raises(ValidationError, match="wikidata_timeout"):
        Settings(wikidata_timeout=timeout)


def test_reads_timeout_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIKIDATA_TIMEOUT", "25")

    assert Settings().wikidata_timeout == 25


def test_rejects_invalid_environment_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIKIDATA_TIMEOUT", "-1")

    with pytest.raises(ValidationError, match="wikidata_timeout"):
        Settings()