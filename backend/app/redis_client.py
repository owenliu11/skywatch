import asyncio
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings

log = logging.getLogger(__name__)

_redis: Redis | None = None

# A dependency blip during a deploy shouldn't crash-loop the API.
_CONNECT_ATTEMPTS = 5
_CONNECT_BACKOFF_S = 2.0


async def connect_redis() -> None:
    global _redis

    client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
    )

    last_exc: Exception | None = None
    for attempt in range(1, _CONNECT_ATTEMPTS + 1):
        try:
            await client.ping()
            _redis = client
            return
        except (RedisError, OSError) as exc:
            last_exc = exc
            log.warning(
                "redis connect failed (attempt %d/%d): %s",
                attempt,
                _CONNECT_ATTEMPTS,
                exc,
            )
            if attempt < _CONNECT_ATTEMPTS:
                await asyncio.sleep(_CONNECT_BACKOFF_S * attempt)

    await client.aclose()
    raise RuntimeError(
        f"could not connect to Redis after {_CONNECT_ATTEMPTS} attempts"
    ) from last_exc


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

def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis is not connected")

    return _redis