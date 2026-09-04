import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """
    Create the application's PostgreSQL connection pool.

    There should be exactly one pool per process.
    """
    global _pool

    if _pool is not None:
        return _pool

    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )

    return _pool


def pool() -> asyncpg.Pool:
    """
    Return the active database pool.

    Fail immediately if the pool has not been initialized.
    """
    if _pool is None:
        raise RuntimeError("Database pool has not been initialized")

    return _pool


async def close_pool() -> None:
    """Close the database connection pool."""
    global _pool

    if _pool is not None:
        await _pool.close()
        _pool = None


async def check_db() -> bool:
    """Return True if PostgreSQL is reachable."""
    if _pool is None:
        return False

    try:
        async with _pool.acquire() as connection:
            result = await connection.fetchval("SELECT 1")
            return result == 1
    except Exception:
        return False


# Temporary compatibility aliases.
# We can remove these after all existing call sites use the new names.
async def connect_db() -> None:
    await init_pool()


async def disconnect_db() -> None:
    await close_pool()