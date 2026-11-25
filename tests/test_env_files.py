"""The example env files list every setting, and nothing else (#101)."""

import re
from pathlib import Path

import pytest

from entity_resolver.config import Settings

ROOT = Path(__file__).resolve().parents[1]
FILES = [".env.example", ".env.production.example"]
# "KEY=value" or "# KEY=value" (a setting left at its default on purpose).
_ASSIGNMENT = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=")


def _keys(name: str) -> list[str]:
    """Every setting name assigned in the env file ``name``, active or commented."""
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return [m.group(1) for line in lines if (m := _ASSIGNMENT.match(line))]


@pytest.mark.parametrize("name", FILES)
def test_env_file_lists_exactly_the_settings(name):
    """No setting is missing from the file, and the file has no unknown key."""
    keys = _keys(name)
    fields = {field.upper() for field in Settings.model_fields}
    assert sorted(fields - set(keys)) == [], f"missing from {name}"
    assert sorted(set(keys) - fields) == [], f"unknown keys in {name}"
    assert len(keys) == len(set(keys)), f"a key appears twice in {name}"


def test_development_example_loads_as_is():
    """Copying .env.example to .env gives a valid development configuration."""
    config = Settings(_env_file=ROOT / ".env.example")
    assert config.environment == "development"


def test_production_example_loads_once_the_secrets_are_filled_in(tmp_path):
    """The production file is valid after the operator sets API_KEY.

    ALIAS_INDEX_PATH points into the Docker volume, which does not exist here, so
    the test points it at a stand-in file.
    """
    index = tmp_path / "companies.json"
    index.write_text("[]", encoding="utf-8")
    config = Settings(
        _env_file=ROOT / ".env.production.example",
        api_key="x" * 64,
        alias_index_path=index,
    )
    assert config.environment == "production" and config.log_format == "json"


def test_production_example_refuses_to_start_without_a_key(tmp_path):
    """Left as shipped (API_KEY empty), the production file fails at startup."""
    index = tmp_path / "companies.json"
    index.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="API_KEY"):
        Settings(_env_file=ROOT / ".env.production.example", alias_index_path=index)


def test_no_env_file_reaches_the_image():
    """.dockerignore keeps every env file out, the examples too."""
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore and ".env.*" in ignore
    assert not any(line.startswith("!") and ".env" in line for line in ignore)
