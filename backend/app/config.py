from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://skywatch:skywatch@db:5432/skywatch"
    redis_url: str = "redis://redis:6379/0"

    daily_credit_budget: int = 4000
    poll_regions: str = "bay_area,socal,nyc"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()