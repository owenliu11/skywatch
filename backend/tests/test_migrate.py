"""Migration runner: checksum enforcement and idempotency."""

from __future__ import annotations

import pytest
from conftest import SingleConnPool

from app.migrate import MigrationChecksumError, apply_migrations

pytestmark = pytest.mark.db


async def test_second_run_is_a_noop(db_conn) -> None:
    # The _migrated_db session fixture already applied every migration to the
    # shared database, so a re-run through the rolled-back test connection
    # should find nothing pending.
    applied = await apply_migrations(SingleConnPool(db_conn))
    assert applied == []


async def test_edited_migration_raises(tmp_path, db_conn) -> None:
    pool = SingleConnPool(db_conn)

    mig = tmp_path / "001_thing.sql"
    mig.write_text("CREATE TABLE _mig_test (id int);\n")
    applied = await apply_migrations(pool, directory=tmp_path)
    assert applied == ["001_thing.sql"]

    # Edit an already-applied file: its checksum no longer matches.
    mig.write_text("CREATE TABLE _mig_test (id int, extra text);\n")
    with pytest.raises(MigrationChecksumError):
        await apply_migrations(pool, directory=tmp_path)
