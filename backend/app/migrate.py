"""Forward-only SQL migration runner.

Files in `migrations/` are applied in filename order, once each, one
transaction per file, recorded in `schema_migrations (version, checksum,
applied_at)`.

The checksum matters: if an already-applied file has been edited, raise rather
than skip. Otherwise your machine and CI silently diverge and you find out in
Week 4.

Usable two ways:
- `await apply_migrations(pool)` from the FastAPI lifespan, so a clean clone
  plus `docker compose up` produces a working database with no manual step.
- `python -m app.migrate` as a standalone command.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_ENSURE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MigrationChecksumError(RuntimeError):
    """An already-applied migration file has changed on disk."""


def _migration_files(directory: Path | None = None) -> list[Path]:
    directory = directory or _MIGRATIONS_DIR
    return sorted(directory.glob("*.sql"))


def _checksum(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def apply_migrations(
    pool: asyncpg.Pool,
    *,
    directory: Path | None = None,
) -> list[str]:
    """Apply pending migrations. Return the versions applied this call."""

    files = _migration_files(directory)
    applied: list[str] = []

    async with pool.acquire() as conn:
        await conn.execute(_ENSURE_TABLE)

        recorded = {
            row["version"]: row["checksum"]
            for row in await conn.fetch(
                "SELECT version, checksum FROM schema_migrations"
            )
        }

        for path in files:
            version = path.name
            body = path.read_text(encoding="utf-8")
            digest = _checksum(body)

            if version in recorded:
                if recorded[version] != digest:
                    raise MigrationChecksumError(
                        f"{version} was already applied with checksum "
                        f"{recorded[version][:12]}..., but the file on disk now "
                        f"hashes to {digest[:12]}.... Migrations are immutable; "
                        f"add a new file instead of editing this one."
                    )
                continue

            log.info("applying migration %s", version)
            async with conn.transaction():
                await conn.execute(body)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) "
                    "VALUES ($1, $2)",
                    version,
                    digest,
                )
            applied.append(version)

    if applied:
        log.info("applied %d migration(s): %s", len(applied), ", ".join(applied))
    else:
        log.info("no pending migrations")

    return applied


async def main() -> None:
    from app.db import close_pool, init_pool
    from app.logging_config import configure_logging

    configure_logging()
    # When run as `python -m app.migrate` the module logger is named
    # "__main__"; log under the app namespace so LOG_LEVEL applies.
    global log
    log = logging.getLogger("app.migrate")

    pool = await init_pool()
    try:
        await apply_migrations(pool)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
