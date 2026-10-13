from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://ps_user:ps_local_dev@localhost:5432/prompt_shields"
    # Per-tenant /ingest/events requests per minute; 0 or less disables it.
    rate_limit_per_minute: int = 100
    # Rows in partner_audit_log older than this are deleted by
    # `python -m collector.audit purge`.
    audit_retention_days: int = 365
    # Data retention (#85). A tenant's settings["retention_days"] overrides the
    # default. Nothing is purged until an operator runs the purge (CLI or the
    # /api/v1/operator endpoints).
    retention_days_default: int = 365
    retention_batch_size: int = 5000
    # Operator token for /api/v1/operator/*. Empty disables those endpoints.
    admin_token: str = ""

    # Semantic search embeddings; see collector/embeddings.py and the README.
    # auto | openai | local | none. auto = openai if OPENAI_API_KEY is set, else none.
    embeddings_backend: str = "auto"
    embeddings_model: str | None = None  # backend default when unset
    embeddings_openai_base_url: str = "https://api.openai.com/v1"
    embeddings_cache_dir: str | None = None  # fastembed model cache (local backend)

    model_config = {"env_prefix": "PS_"}


settings = Settings()
