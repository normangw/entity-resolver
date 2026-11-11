import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, FilePath
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_INDEX = Path(__file__).parent / "data" / "companies.json"


class Settings(BaseSettings):
    """Load and validate application configuration at startup."""

    app_name: str = Field(default="Entity Resolver API", min_length=1)
    app_version: str = Field(default="0.1.0", min_length=1)
    debug: bool = False
    wikidata_timeout: int = Field(default=10, gt=0)
    # On by default so brands and subsidiaries (YouTube, Instagram) resolve. Tests set
    # WIKIDATA_FALLBACK=false in tests/conftest.py so CI never touches the network.
    wikidata_fallback: bool = True
    # Seconds to remember a Wikidata answer (0 disables); it throttles quickly.
    wikidata_cache_ttl: float = Field(default=3600, ge=0, allow_inf_nan=False)

    alias_index_path: FilePath = _DEFAULT_INDEX
    match_threshold: float = Field(default=0.9, ge=0, le=1)
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

    # --- Abuse and cost protection -------------------------------------------
    # Optional shared secret, sent as the X-API-Key header. Unset means no key is
    # needed (the public demo). When set it is required on /resolve/batch and
    # /metrics, and on every other data endpoint too if api_key_protect_all is true
    # (the web page cannot send a key, so that setting turns the page off).
    api_key: str | None = None
    api_key_protect_all: bool = False
    # Requests per client address per minute; 0 turns a limit off.
    rate_limit_resolve_per_min: int = Field(default=120, ge=0)
    rate_limit_batch_per_min: int = Field(default=10, ge=0)
    rate_limit_upload_per_min: int = Field(default=20, ge=0)
    # Largest JSON body accepted (uploads have their own limit above).
    max_body_bytes: int = Field(default=256 * 1024, gt=0)
    batch_max_names: int = Field(default=100, gt=0)
    # After this long a batch stops calling Wikidata and marks the names it has not
    # settled locally "retry", so the call finishes inside the proxy's timeout.
    batch_budget_seconds: float = Field(default=60.0, gt=0)
    # Wikidata lookups allowed at once across all requests; later ones wait up to
    # wikidata_queue_seconds, then get a 503, so a script cannot flood Wikidata.
    wikidata_max_concurrent: int = Field(default=4, gt=0)
    wikidata_queue_seconds: float = Field(default=5.0, gt=0)
    wikidata_retries: int = Field(default=2, ge=0, le=5)
    # After this many failed lookups in a row Wikidata is not called for
    # wikidata_breaker_seconds; requests get a 503 at once instead of waiting.
    wikidata_breaker_failures: int = Field(default=5, gt=0)
    wikidata_breaker_seconds: float = Field(default=30.0, gt=0)

    # --- Observability --------------------------------------------------------
    log_format: Literal["json", "text"] = "json"
    log_level: str = "INFO"
    # Identifies the deployed build in /health. Set at image build time, e.g.
    # BUILD_ID=$(git rev-parse --short HEAD) docker compose up -d --build
    build_id: str = "unknown"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
    )


settings = Settings()
