from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://ps_user:ps_local_dev@localhost:5432/prompt_shields"
    # Per-tenant /ingest/events requests per minute; 0 or less disables it.
    rate_limit_per_minute: int = 100

    # Semantic search embeddings; see collector/embeddings.py and the README.
    # auto | openai | local | none. auto = openai if OPENAI_API_KEY is set, else none.
    embeddings_backend: str = "auto"
    embeddings_model: str | None = None  # backend default when unset
    embeddings_openai_base_url: str = "https://api.openai.com/v1"
    embeddings_cache_dir: str | None = None  # fastembed model cache (local backend)

    model_config = {"env_prefix": "PS_"}


settings = Settings()
