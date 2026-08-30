from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings

_redis: Redis | None = None


async def connect_redis() -> None:
    global _redis

    _redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
    )

    await _redis.ping()


async def disconnect_redis() -> None:
    global _redis

    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def check_redis() -> bool:
    if _redis is None:
        return False

    try:
        return bool(await _redis.ping())

    except RedisError:
        return False