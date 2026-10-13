from pathlib import Path

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


@pytest.mark.parametrize("threshold", [-0.1, 1.1, float("nan")])
def test_rejects_invalid_match_threshold(threshold: float) -> None:
    with pytest.raises(ValidationError, match="match_threshold"):
        Settings(_env_file=None, match_threshold=threshold)


@pytest.mark.parametrize("threshold", [0.0, 1.0])
def test_accepts_threshold_boundaries(threshold: float) -> None:
    assert (
        Settings(_env_file=None, match_threshold=threshold).match_threshold == threshold
    )


@pytest.mark.parametrize("delay", [-1.0, float("inf"), float("nan")])
def test_rejects_invalid_model_delay(delay: float) -> None:
    with pytest.raises(ValidationError, match="model_delay_seconds"):
        Settings(_env_file=None, model_delay_seconds=delay)


def test_reads_resolver_settings_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    index = tmp_path / "companies.json"
    index.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("ALIAS_INDEX_PATH", str(index))
    monkeypatch.setenv("MATCH_THRESHOLD", "0.9")
    monkeypatch.setenv("MODEL_DELAY_SECONDS", "0.2")

    config = Settings(_env_file=None)

    assert config.alias_index_path == index
    assert config.match_threshold == 0.9
    assert config.model_delay_seconds == 0.2


def test_rejects_missing_alias_index(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="alias_index_path"):
        Settings(_env_file=None, alias_index_path=tmp_path / "missing.json")


def test_rejects_directory_as_alias_index(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="alias_index_path"):
        Settings(_env_file=None, alias_index_path=tmp_path)
