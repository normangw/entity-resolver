from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Load and validate application configuration at startup."""

    app_name: str = Field(default="Entity Resolver API", min_length=1)
    app_version: str = Field(default="0.1.0", min_length=1)
    debug: bool = False
    wikidata_timeout: int = Field(default=10, gt=0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
    )


settings = Settings()
