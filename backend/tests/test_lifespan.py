import asyncio

import pytest
from fastapi import FastAPI

import app.main as main_module


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    fake_pool = object()

    writer_started = asyncio.Event()

    async def fake_connect_db() -> None:
        events.append("db_connected")

    async def fake_connect_redis() -> None:
        events.append("redis_connected")

    async def fake_disconnect_db() -> None:
        events.append("db_disconnected")

    async def fake_disconnect_redis() -> None:
        events.append("redis_disconnected")

    def fake_db_pool():
        events.append("db_pool_requested")
        return fake_pool

    async def fake_apply_migrations(pool) -> None:
        assert pool is fake_pool
        events.append("migrations_applied")

    async def fake_writer_loop() -> None:
        events.append("writer_started")
        writer_started.set()

        try:
            # Behave like the real writer: stay alive until cancelled.
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("writer_stopped")
            raise

    class FakeWriteQueue:
        async def join(self) -> None:
            events.append("queue_drained")

        def qsize(self) -> int:
            return 0

    class FakeScheduler:
        def __init__(self, **kwargs) -> None:
            self.run_started = asyncio.Event()
            self.stop_requested = asyncio.Event()

        async def run(self) -> None:
            events.append("scheduler_started")
            self.run_started.set()

            await self.stop_requested.wait()

            events.append("scheduler_run_finished")

        async def stop(self) -> None:
            events.append("scheduler_stopped")
            self.stop_requested.set()

            # Give run() a chance to observe the stop request.
            await asyncio.sleep(0)

    monkeypatch.setattr(
        main_module,
        "connect_db",
        fake_connect_db,
    )

    monkeypatch.setattr(
        main_module,
        "connect_redis",
        fake_connect_redis,
    )

    monkeypatch.setattr(
        main_module,
        "disconnect_db",
        fake_disconnect_db,
    )

    monkeypatch.setattr(
        main_module,
        "disconnect_redis",
        fake_disconnect_redis,
    )

    monkeypatch.setattr(
        main_module,
        "db_pool",
        fake_db_pool,
    )

    monkeypatch.setattr(
        main_module,
        "apply_migrations",
        fake_apply_migrations,
    )

    monkeypatch.setattr(
        main_module,
        "writer_loop",
        fake_writer_loop,
    )

    monkeypatch.setattr(
        main_module,
        "write_queue",
        FakeWriteQueue(),
    )

    monkeypatch.setattr(
        main_module,
        "RegionScheduler",
        FakeScheduler,
    )

    test_app = FastAPI(
        lifespan=main_module.lifespan,
    )

    async with test_app.router.lifespan_context(test_app):
        scheduler = test_app.state.region_scheduler

        await asyncio.wait_for(
            scheduler.run_started.wait(),
            timeout=1.0,
        )

        await asyncio.wait_for(
            writer_started.wait(),
            timeout=1.0,
        )

        assert test_app.state.opensky_client is not None

        assert test_app.state.region_scheduler is scheduler

        assert test_app.state.scheduler_task is not None

        assert test_app.state.writer_task is not None

        assert "db_connected" in events
        assert "db_pool_requested" in events
        assert "migrations_applied" in events
        assert "redis_connected" in events
        assert "writer_started" in events
        assert "scheduler_started" in events

        # Shutdown has not happened yet.
        assert "scheduler_stopped" not in events
        assert "writer_stopped" not in events
        assert "db_disconnected" not in events
        assert "redis_disconnected" not in events

    # Shutdown behavior.
    assert "scheduler_stopped" in events
    assert "scheduler_run_finished" in events
    assert "queue_drained" in events
    assert "writer_stopped" in events
    assert "redis_disconnected" in events
    assert "db_disconnected" in events

    # Database must connect before migrations are applied.
    assert events.index("db_connected") < events.index(
        "migrations_applied"
    )

    # Migrations happen before Redis/startup services continue.
    assert events.index("migrations_applied") < events.index(
        "redis_connected"
    )

    # Infrastructure must be ready before scheduler operation.
    assert events.index("db_connected") < events.index(
        "scheduler_started"
    )

    assert events.index("redis_connected") < events.index(
        "scheduler_started"
    )

    # Scheduler stops before writer shutdown.
    assert events.index("scheduler_stopped") < events.index(
        "queue_drained"
    )

    # Queue drains before writer is cancelled.
    assert events.index("queue_drained") < events.index(
        "writer_stopped"
    )

    # Background services stop before infrastructure disconnects.
    assert events.index("writer_stopped") < events.index(
        "redis_disconnected"
    )

    assert events.index("writer_stopped") < events.index(
        "db_disconnected"
    )