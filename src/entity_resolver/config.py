from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_INDEX = Path(__file__).parent / "data" / "companies.json"


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    app_name: str = "Entity Resolver API"
    app_version: str = "0.1.0"
    debug: bool = False
    wikidata_timeout: int = 10

    # Model settings: moving or swapping the model is a .env change + restart.
    alias_index_path: Path = _DEFAULT_INDEX
    match_threshold: float = 0.8
    model_delay_seconds: float = 0.0  # >0 simulates slow inference for the audit

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
