from pathlib import Path

import pytest
from pydantic import ValidationError

from entity_resolver.config import Settings


@pytest.mark.parametrize("timeout", [0, -1])
def test_rejects_nonpositive_timeout(timeout: int) -> None:
    with pytest.raises(ValidationError, match="wikidata_timeout"):
        Settings(_env_file=None, wikidata_timeout=timeout)


def test_reads_timeout_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIKIDATA_TIMEOUT", "25")

    assert Settings(_env_file=None).wikidata_timeout == 25


def test_rejects_invalid_environment_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIKIDATA_TIMEOUT", "-1")

    with pytest.raises(ValidationError, match="wikidata_timeout"):
        Settings(_env_file=None)


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


# --- profiles and cross-field checks (#101) ------------------------------------


def test_production_requires_an_api_key() -> None:
    """ENVIRONMENT=production without API_KEY stops at startup."""
    with pytest.raises(ValidationError, match="API_KEY"):
        Settings(_env_file=None, environment="production")


def test_production_rejects_a_short_api_key() -> None:
    """A placeholder-length key is refused in production."""
    with pytest.raises(ValidationError, match="API_KEY"):
        Settings(_env_file=None, environment="production", api_key="change-me")


def test_production_requires_json_logs() -> None:
    """Text logs are refused in production; they cannot be searched by field."""
    with pytest.raises(ValidationError, match="LOG_FORMAT"):
        Settings(
            _env_file=None,
            environment="production",
            api_key="k" * 32,
            log_format="text",
        )


def test_production_with_a_key_and_json_logs_is_valid() -> None:
    """The documented production settings pass."""
    config = Settings(_env_file=None, environment="production", api_key="k" * 32)
    assert config.environment == "production"


def test_development_needs_no_key() -> None:
    """The default profile runs open, for local work and the CI smoke test."""
    assert Settings(_env_file=None).api_key is None


def test_empty_api_key_means_no_key() -> None:
    """``API_KEY=`` in a .env is the same as leaving it out."""
    assert Settings(_env_file=None, api_key="").api_key is None


@pytest.mark.parametrize(("low", "threshold"), [(0.9, 0.9), (0.95, 0.9)])
def test_gleif_threshold_must_be_below_the_match_threshold(
    low: float, threshold: float
) -> None:
    """With GLEIF on, a floor at or above the match threshold could never run."""
    with pytest.raises(ValidationError, match="gleif_low_threshold"):
        Settings(
            _env_file=None,
            gleif_enabled=True,
            gleif_low_threshold=low,
            match_threshold=threshold,
        )
    # With GLEIF off the floor is unused, so any match threshold is allowed.
    Settings(_env_file=None, gleif_low_threshold=low, match_threshold=threshold)


def test_rejects_an_unknown_environment_and_scorer() -> None:
    """Only the listed profiles and scorers are accepted."""
    with pytest.raises(ValidationError, match="environment"):
        Settings(_env_file=None, environment="staging")
    with pytest.raises(ValidationError, match="scorer"):
        Settings(_env_file=None, scorer="levenshtein")


def test_rejects_a_wikidata_url_that_is_not_http() -> None:
    """WIKIDATA_API_URL must be an http(s) URL."""
    with pytest.raises(ValidationError, match="wikidata_api_url"):
        Settings(_env_file=None, wikidata_api_url="file:///etc/passwd")


def test_rejects_a_nonpositive_deadline() -> None:
    """The lookup deadline must be positive."""
    with pytest.raises(ValidationError, match="wikidata_deadline_seconds"):
        Settings(_env_file=None, wikidata_deadline_seconds=0)


def test_unknown_key_in_env_file_is_refused(tmp_path: Path) -> None:
    """A typo in .env stops the app instead of being silently ignored."""
    env = tmp_path / ".env"
    env.write_text("MATCH_TRESHOLD=0.5\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="match_treshold"):
        Settings(_env_file=env)
