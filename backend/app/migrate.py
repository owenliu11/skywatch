import asyncio
import hashlib
from pathlib import Path

import asyncpg

from app.db import close_pool, init_pool

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def calculate_checksum(contents: str) -> str:
    """Return a SHA-256 checksum for a migration file."""
    return hashlib.sha256(contents.encode("utf-8")).hexdigest()


async def ensure_migrations_table(connection: asyncpg.Connection) -> None:
    """Create the migration history table if it does not already exist."""
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     TEXT PRIMARY KEY,
            checksum    TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


async def apply_migrations(pool: asyncpg.Pool) -> None:
    """
    Apply unapplied SQL migrations in filename order.

    Already-applied migrations must have the same checksum.
    If an applied migration was edited, fail loudly.
    """
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    async with pool.acquire() as connection:
        await ensure_migrations_table(connection)

    for migration_file in migration_files:
        version = migration_file.name
        contents = migration_file.read_text(encoding="utf-8")
        checksum = calculate_checksum(contents)

        async with pool.acquire() as connection:
            existing = await connection.fetchrow(
                """
                SELECT checksum
                FROM schema_migrations
                WHERE version = $1
                """,
                version,
            )

            if existing is not None:
                if existing["checksum"] != checksum:
                    raise RuntimeError(
                        f"Migration {version} was modified after being applied"
                    )

                print(f"Skipping migration {version} (already applied)")
                continue

            print(f"Applying migration {version}")

            async with connection.transaction():
                await connection.execute(contents)

                await connection.execute(
                    """
                    INSERT INTO schema_migrations (version, checksum)
                    VALUES ($1, $2)
                    """,
                    version,
                    checksum,
                )

            print(f"Applied migration {version}")


async def main() -> None:
    database_pool = await init_pool()

    try:
        await apply_migrations(database_pool)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())