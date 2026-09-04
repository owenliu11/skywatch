from collections.abc import AsyncIterator

import asyncpg
import pytest

from app.config import settings
from app.migrate import apply_migrations


@pytest.fixture
async def db_connection() -> AsyncIterator[asyncpg.Connection]:
    """
    Provide one real TimescaleDB connection per test.

    Each test runs inside an outer transaction which is rolled back,
    so integration tests cannot permanently modify the development DB.
    """

    test_pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=1,
        max_size=2,
        command_timeout=30,
    )

    try:
        # Make sure the test database has the current schema.
        await apply_migrations(test_pool)

        async with test_pool.acquire() as connection:
            transaction = connection.transaction()

            await transaction.start()

            try:
                yield connection
            finally:
                await transaction.rollback()

    finally:
        await test_pool.close()