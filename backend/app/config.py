from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]
class Settings(BaseSettings):
    
    # OpenSky
    opensky_client_id: str = ""
    opensky_client_secret: str = ""
    
    # Database / Redis
    database_url: str = "postgresql://skywatch:skywatch@db:5432/skywatch"
    redis_url: str = "redis://redis:6379/0"
    
    # SkyWatch
    daily_credit_budget: int = 4000
    poll_regions: str = "bay_area,socal,nyc"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file= BASE_DIR / ".env",
        extra="ignore",
    )


settings = Settings()