"""Shared fixtures.

Phase 2 storage tests run against a real TimescaleDB -- mocking Postgres here
would test nothing worth testing. Point `TEST_DATABASE_URL` at a
`timescale/timescaledb-ha` instance (compose publishes one on 5433). Tests
marked `db` are skipped, not failed, when it is unreachable.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
import pytest_asyncio

from app.migrate import apply_migrations

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://skywatch:skywatch@localhost:5433/skywatch",
)


async def _migrate_once() -> None:
    pool = await asyncpg.create_pool(TEST_DATABASE_URL, min_size=1, max_size=1)
    try:
        await apply_migrations(pool)
    finally:
        await pool.close()


@pytest.fixture(scope="session")
def _migrated_db() -> None:
    """Run migrations once per session; skip db tests if the DB is unreachable."""
    try:
        asyncio.run(_migrate_once())
    except (asyncpg.PostgresError, OSError) as exc:
        pytest.skip(f"TEST_DATABASE_URL unreachable ({exc}); skipping db tests")


@pytest_asyncio.fixture
async def db_conn(_migrated_db: None):
    """A connection wrapped in a transaction that is rolled back after the test."""
    conn = await asyncpg.connect(TEST_DATABASE_URL)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


class _AcquireCtx:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class SingleConnPool:
    """Minimal pool stand-in so StateWriter writes through the test's txn."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self._conn)


@pytest.fixture
def writer_pool(db_conn, monkeypatch: pytest.MonkeyPatch) -> SingleConnPool:
    """Redirect `app.ingest.writer.pool()` at the rolled-back test connection."""
    fake = SingleConnPool(db_conn)
    monkeypatch.setattr("app.ingest.writer.pool", lambda: fake)
    return fake
