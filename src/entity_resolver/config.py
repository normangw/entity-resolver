"""Application settings, read from the environment and ``.env`` at startup.

Every setting is listed in both ``.env.example`` (development) and
``.env.production.example`` (deployment); ``tests/test_env_files.py`` fails when
one is missing. A bad value stops the app at startup with the setting's name.
"""

import tempfile
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, FilePath, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_INDEX = Path(__file__).parent / "data" / "companies.json"
# Keys shorter than this are refused in production (``openssl rand -hex 32`` is 64).
_MIN_KEY_LENGTH = 16


class Settings(BaseSettings):
    """Load and validate application configuration at startup."""

    # "production" turns on the checks in ``_check_profile``: an API key and JSON
    # logs are required. The CI smoke test and local runs use "development".
    environment: Literal["development", "production"] = "development"
    app_name: str = Field(default="Entity Resolver API", min_length=1)
    # The version is not a setting: it lives only in pyproject.toml and is read from
    # the installed package (entity_resolver.__version__), so it cannot drift.
    debug: bool = False
    # Seconds for one Wikidata API call. A lookup makes 2 to 5 calls.
    wikidata_timeout: int = Field(default=10, gt=0)
    # On by default so brands and subsidiaries (YouTube, Instagram) resolve. Tests set
    # WIKIDATA_FALLBACK=false in tests/conftest.py so CI never touches the network.
    wikidata_fallback: bool = True
    # Seconds to remember a Wikidata answer (0 disables); it throttles quickly.
    wikidata_cache_ttl: float = Field(default=3600, ge=0, allow_inf_nan=False)
    # The MediaWiki API to ask; another Wikibase (or a mock) is a .env change.
    wikidata_api_url: str = Field(
        default="https://www.wikidata.org/w/api.php", pattern=r"^https?://\S+$"
    )
    # Seconds one whole lookup (all its API calls, retries and waits) may take. No
    # new call starts after it, so a lookup ends within this plus one
    # wikidata_timeout. See docs/time-limits.md.
    wikidata_deadline_seconds: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    # Longest Retry-After from Wikidata's 429 that is waited out (once per lookup).
    # A longer one becomes our 503 with the same Retry-After.
    wikidata_max_retry_after: float = Field(default=2.0, ge=0, allow_inf_nan=False)

    alias_index_path: FilePath = _DEFAULT_INDEX
    # The RapidFuzz scorer for the local index. Changing it is a .env edit and a
    # restart; re-check MATCH_THRESHOLD with scripts/evaluate.py when you do.
    scorer: Literal["ratio", "WRatio", "token_set_ratio"] = "ratio"
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
    # Optional shared secret, sent as the X-API-Key header or as
    # "Authorization: Bearer <key>" (what Prometheus sends). Unset means no key is
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

    # --- Reverse proxy (read by Caddy through compose.yml, not by the app) -------
    # Declared here because Settings refuses unknown keys in .env, and compose reads
    # these from the same .env. SITE_ADDRESS is the public DNS name, which makes
    # Caddy get a Let's Encrypt certificate; the default ":80" is plain HTTP for a
    # local `docker compose up`. ACME_CA switches to the staging CA if Let's Encrypt
    # rate-limits us.
    site_address: str = ":80"
    acme_ca: str = "https://acme-v02.api.letsencrypt.org/directory"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
    )

    @model_validator(mode="after")
    def _check_profile(self) -> Self:
        """Refuse combinations that are wrong, failing at startup, not at use.

        An empty ``API_KEY=`` means no key. In production a key of at least 16
        characters and JSON logs are required. With GLEIF on, ``gleif_low_threshold``
        must be below ``match_threshold``, or the tiebreak could never run.
        """
        if self.api_key == "":
            self.api_key = None
        if self.gleif_enabled and self.gleif_low_threshold >= self.match_threshold:
            raise ValueError(
                "gleif_low_threshold must be below match_threshold "
                f"({self.gleif_low_threshold} >= {self.match_threshold})"
            )
        if self.environment == "production":
            if self.api_key is None or len(self.api_key) < _MIN_KEY_LENGTH:
                raise ValueError(
                    "ENVIRONMENT=production needs API_KEY of at least "
                    f"{_MIN_KEY_LENGTH} characters (openssl rand -hex 32)"
                )
            if self.log_format != "json":
                raise ValueError("ENVIRONMENT=production needs LOG_FORMAT=json")
        return self


settings = Settings()
