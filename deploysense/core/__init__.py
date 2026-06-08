"""
DeploySense — Application Configuration

WHY THIS EXISTS:
Every service needs configuration. Instead of scattered os.getenv() calls,
we centralize all config into a single Pydantic Settings class. This gives us:
  1. Type validation at startup (fail fast on bad config)
  2. Environment variable parsing with sensible defaults
  3. .env file support for local development
  4. A single source of truth for "what can be configured"

TRADEOFF:
We use a single Settings class for all services. This means the API service
loads Risk Engine URL config even though it doesn't need it internally.
At MVP scale this is fine — the alternative (per-service settings) adds
maintenance overhead without benefit until services are truly independent.

ALTERNATIVES CONSIDERED:
  - Raw os.getenv(): No validation, scattered across codebase
  - python-decouple: Less type safety than Pydantic
  - dynaconf: More powerful but more complex than we need
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ─── Application ─────────────────────────────────────────────────────
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    secret_key: str = "change-me-in-production"

    # ─── Database ────────────────────────────────────────────────────────
    database_url: str = (
        "postgresql+asyncpg://deploysense:deploysense_dev@localhost:5432/deploysense"
    )
    database_url_sync: str = (
        "postgresql://deploysense:deploysense_dev@localhost:5432/deploysense"
    )
    database_pool_size: int = 20
    database_max_overflow: int = 10

    # ─── Redis ───────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ─── GitHub ──────────────────────────────────────────────────────────
    github_client_id: str = ""
    github_client_secret: str = ""
    github_webhook_secret: str = ""

    # ─── Frontend ────────────────────────────────────────────────────────
    frontend_url: str = "http://localhost:3001"

    # ─── Backend ─────────────────────────────────────────────────────────
    backend_port: int = 8000

    # ─── Inter-Service Communication ─────────────────────────────────────
    risk_engine_url: str = "http://localhost:8001"

    # ─── Observability ───────────────────────────────────────────────────
    otel_service_name: str = "deploysense"
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_traces_enabled: bool = False

    # ─── AI Engine (Phase 2) ─────────────────────────────────────────────
    ai_api_base: str = "https://api.openai.com/v1"
    ai_api_key: str = ""
    ai_model: str = "gpt-4o-mini"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Cached settings factory.

    WHY lru_cache: Settings are immutable after startup. Parsing .env and
    environment variables on every request is wasteful. Cache once, use everywhere.
    """
    return Settings()
