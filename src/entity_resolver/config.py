from pathlib import Path

from pydantic import Field, FilePath
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_INDEX = Path(__file__).parent / "data" / "companies.json"


class Settings(BaseSettings):
    """Load and validate application configuration at startup."""

    app_name: str = Field(default="Entity Resolver API", min_length=1)
    app_version: str = Field(default="0.1.0", min_length=1)
    debug: bool = False
    wikidata_timeout: int = Field(default=10, gt=0)
    # Off by default so tests and CI never touch the network.
    wikidata_fallback: bool = False

    alias_index_path: FilePath = _DEFAULT_INDEX
    match_threshold: float = Field(default=0.8, ge=0, le=1)
    model_delay_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
    )


settings = Settings()
