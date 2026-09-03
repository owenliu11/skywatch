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

    # Run pending SQL migrations from the FastAPI lifespan on startup. Handy
    # for `docker compose up` on a clean clone; set false to manage schema
    # out of band with `python -m app.migrate`.
    run_migrations_on_startup: bool = True

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
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
    "socal": Region(
        name="socal",
        lamin=32.5,
        lomin=-119.0,
        lamax=35.0,
        lomax=-116.5,
        base_interval_s=20.0,
    ),
    "nyc": Region(
        name="nyc",
        lamin=40.0,
        lomin=-75.0,
        lamax=41.5,
        lomax=-73.0,
        base_interval_s=20.0,
    ),
}

settings = Settings()


def active_regions() -> list[Region]:
    """Regions the scheduler should poll, from the POLL_REGIONS setting.

    Order follows POLL_REGIONS, not the REGIONS declaration. An unknown name
    is a configuration error and fails loudly at startup rather than silently
    polling fewer regions than intended.
    """

    names = [
        chunk.strip()
        for chunk in settings.poll_regions.split(",")
        if chunk.strip()
    ]

    unknown = [name for name in names if name not in REGIONS]
    if unknown:
        raise ValueError(
            f"POLL_REGIONS names unknown region(s): {', '.join(unknown)}; "
            f"known regions are {', '.join(sorted(REGIONS))}"
        )

    if not names:
        raise ValueError("POLL_REGIONS is empty; nothing to poll")

    return [REGIONS[name] for name in names]
