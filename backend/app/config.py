from dataclasses import dataclass
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


@dataclass(frozen=True)
class Region:
    name: str
    lamin: float
    lomin: float
    lamax: float
    lomax: float
    base_interval_s: float


MIN_POLL_INTERVAL_S = 5.0
MAX_POLL_INTERVAL_S = 300.0


REGIONS = {
    "bay_area": Region(
        name="bay_area",
        lamin=37.0,
        lomin=-123.0,
        lamax=38.5,
        lomax=-121.5,
        base_interval_s=15.0,
    ),
}

settings = Settings()