"""One asyncpg pool per process.

Created in the FastAPI lifespan and in the standalone `python -m app.migrate`
runner so both share connection settings. `pool()` fails loudly if the pool
was never initialised rather than deferring the error to the first query.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from app.config import settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

# max_size=10 is generous: the writer holds one connection for the length of a
# batch, API handlers take one each. Pool exhaustion is a signal the writer is
# holding a connection across an await it shouldn't.
_MIN_SIZE = 2
_MAX_SIZE = 10
_COMMAND_TIMEOUT_S = 30.0

# A dependency blip during a deploy shouldn't crash-loop the API.
_CONNECT_ATTEMPTS = 5
_CONNECT_BACKOFF_S = 2.0


async def init_pool() -> asyncpg.Pool:
    """Create the process-wide pool, retrying a few times on failure."""

    global _pool

    if _pool is not None:
        return _pool

    last_exc: Exception | None = None

    for attempt in range(1, _CONNECT_ATTEMPTS + 1):
        try:
            _pool = await asyncpg.create_pool(
                dsn=settings.database_url,
                min_size=_MIN_SIZE,
                max_size=_MAX_SIZE,
                command_timeout=_COMMAND_TIMEOUT_S,
            )
            return _pool

        except (asyncpg.PostgresError, OSError) as exc:
            last_exc = exc
            log.warning(
                "database pool init failed (attempt %d/%d): %s",
                attempt,
                _CONNECT_ATTEMPTS,
                exc,
            )
            if attempt < _CONNECT_ATTEMPTS:
                await asyncio.sleep(_CONNECT_BACKOFF_S * attempt)

    raise RuntimeError(
        f"could not connect to the database after {_CONNECT_ATTEMPTS} attempts"
    ) from last_exc


def pool() -> asyncpg.Pool:
    """Return the process-wide pool. Raise if init_pool has not run."""

    if _pool is None:
        raise RuntimeError("database pool is not initialised; call init_pool() first")

    return _pool


async def close_pool() -> None:
    global _pool

    if _pool is not None:
        await _pool.close()
        _pool = None


async def check_db() -> bool:
    """Health probe. Returns False rather than raising when the DB is down."""

    if _pool is None:
        return False

    try:
        async with _pool.acquire() as connection:
            result = await connection.fetchval("SELECT 1")

        return result == 1

    except (asyncpg.PostgresError, OSError):
        return False
