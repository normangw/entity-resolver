import tempfile
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

    alias_index_path: FilePath = _DEFAULT_INDEX
    match_threshold: float = Field(default=0.8, ge=0, le=1)
    model_delay_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)

    # GLEIF tiebreak for low-confidence matches. Off by default: enabling it makes
    # network calls to the public GLEIF API.
    gleif_enabled: bool = False
    gleif_low_threshold: float = Field(default=0.6, ge=0, le=1)
    gleif_daily_budget: int = Field(default=1000, gt=0)
    gleif_timeout: float = Field(default=5.0, gt=0)

    # Spreadsheet uploads. Files live in upload_dir only until upload_ttl_seconds
    # after upload; nothing from them is logged.
    upload_dir: Path = Path(tempfile.gettempdir()) / "entity-resolver-uploads"
    upload_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    upload_max_rows: int = Field(default=5000, gt=0)
    upload_ttl_seconds: int = Field(default=3600, gt=0)
    upload_max_running: int = Field(default=3, gt=0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
    )


settings = Settings()
