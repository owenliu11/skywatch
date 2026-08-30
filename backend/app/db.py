import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def connect_db() -> None:
    global _pool

    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=1,
        max_size=5,
    )


async def disconnect_db() -> None:
    global _pool

    if _pool is not None:
        await _pool.close()
        _pool = None


async def check_db() -> bool:
    if _pool is None:
        return False

    try:
        async with _pool.acquire() as connection:
            result = await connection.fetchval("SELECT 1")

        return result == 1

    except asyncpg.PostgresError:
        return False