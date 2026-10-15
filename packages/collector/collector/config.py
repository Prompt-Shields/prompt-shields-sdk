from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://ps_user:ps_local_dev@localhost:5432/prompt_shields"
    # Per-tenant /ingest/events requests per minute; 0 or less disables it.
    rate_limit_per_minute: int = 100
    # Rows in partner_audit_log older than this are deleted by
    # `python -m collector.audit purge`.
    audit_retention_days: int = 365
    # Asset lifecycle: days without telemetry before an asset is marked
    # stale (PS_ASSET_STALE_AFTER_DAYS) and then retired
    # (PS_ASSET_RETIRE_AFTER_DAYS). See collector/lifecycle.py.
    asset_stale_after_days: int = 30
    asset_retire_after_days: int = 90
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

    # Partner change webhooks (#103); see collector/webhooks.py.
    # Key that encrypts subscription signing secrets at rest. Empty disables
    # creating subscriptions and delivering.
    webhook_secret_key: str = ""
    # Dev/test only: allow http:// targets and private, loopback or
    # link-local destinations.
    webhook_allow_http: bool = False
    webhook_allow_private_destinations: bool = False
    # Deadline for one whole delivery attempt. Keep it well under the
    # 5-minute claim lease (webhooks.CLAIM_LEASE).
    webhook_timeout_seconds: float = 10.0
    webhook_max_attempts: int = 8
    # In-process delivery loop interval; 0 disables it (then run
    # `python -m collector.webhooks deliver` from a worker or cron).
    webhook_worker_interval_seconds: float = 0
    webhook_delivery_retention_days: int = 30

    model_config = {"env_prefix": "PS_"}


settings = Settings()
