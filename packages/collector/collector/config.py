from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://ps_user:ps_local_dev@localhost:5432/prompt_shields"
    rate_limit_per_minute: int = 100
    # Rows in partner_audit_log older than this are deleted by
    # `python -m collector.audit purge`.
    audit_retention_days: int = 365

    model_config = {"env_prefix": "PS_"}


settings = Settings()
