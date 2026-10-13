from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://ps_user:ps_local_dev@localhost:5432/prompt_shields"
    rate_limit_per_minute: int = 100
    # Asset lifecycle: days without telemetry before an asset is marked
    # stale (PS_ASSET_STALE_AFTER_DAYS) and then retired
    # (PS_ASSET_RETIRE_AFTER_DAYS). See collector/lifecycle.py.
    asset_stale_after_days: int = 30
    asset_retire_after_days: int = 90

    model_config = {"env_prefix": "PS_"}


settings = Settings()
