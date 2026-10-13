from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://ps_user:ps_local_dev@localhost:5432/prompt_shields"
    # Per-tenant /ingest/events requests per minute; 0 or less disables it.
    rate_limit_per_minute: int = 100

    model_config = {"env_prefix": "PS_"}


settings = Settings()
