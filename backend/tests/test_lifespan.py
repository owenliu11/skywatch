import asyncio

import pytest
from fastapi import FastAPI

import app.main as main_module


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def fake_init_pool() -> object:
        events.append("db_connected")
        return object()

    async def fake_connect_redis() -> None:
        events.append("redis_connected")

    async def fake_close_pool() -> None:
        events.append("db_disconnected")

    async def fake_disconnect_redis() -> None:
        events.append("redis_disconnected")

    async def fake_apply_migrations(_pool: object) -> list[str]:
        events.append("migrations_applied")
        return []

    class FakeScheduler:
        def __init__(self, **kwargs) -> None:
            self.intervals = {"bay_area": 15.0}
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
            await asyncio.sleep(0)

    class FakeWriter:
        def __init__(self) -> None:
            self.run_started = asyncio.Event()

        async def run(self) -> None:
            events.append("writer_started")
            self.run_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("writer_stopped")
                raise

    class FakeClient:
        async def aclose(self) -> None:
            events.append("client_closed")

    monkeypatch.setattr(main_module, "init_pool", fake_init_pool)
    monkeypatch.setattr(main_module, "connect_redis", fake_connect_redis)
    monkeypatch.setattr(main_module, "close_pool", fake_close_pool)
    monkeypatch.setattr(main_module, "disconnect_redis", fake_disconnect_redis)
    monkeypatch.setattr(main_module, "apply_migrations", fake_apply_migrations)
    monkeypatch.setattr(main_module, "pool", lambda: object())
    monkeypatch.setattr(main_module, "RegionScheduler", FakeScheduler)
    monkeypatch.setattr(main_module, "OpenSkyClient", FakeClient)
    monkeypatch.setattr(main_module, "writer", FakeWriter())

    test_app = FastAPI(lifespan=main_module.lifespan)

    async with test_app.router.lifespan_context(test_app):
        scheduler = test_app.state.region_scheduler
        await asyncio.wait_for(scheduler.run_started.wait(), timeout=1.0)
        await asyncio.wait_for(
            test_app.state.writer.run_started.wait(), timeout=1.0
        )

        assert test_app.state.opensky_client is not None
        assert test_app.state.scheduler_task is not None
        assert test_app.state.writer_task is not None

        assert "db_connected" in events
        assert "redis_connected" in events
        assert "migrations_applied" in events
        assert "scheduler_started" in events
        assert "writer_started" in events

        assert "db_disconnected" not in events
        assert "redis_disconnected" not in events

    assert "scheduler_stopped" in events
    assert "writer_stopped" in events
    assert "client_closed" in events
    assert "redis_disconnected" in events
    assert "db_disconnected" in events

    # Infrastructure connects, then migrations, then background tasks.
    assert events.index("db_connected") < events.index("migrations_applied")
    assert events.index("migrations_applied") < events.index("scheduler_started")
    assert events.index("db_connected") < events.index("writer_started")

    # Background tasks stop before infrastructure is torn down.
    assert events.index("scheduler_stopped") < events.index("db_disconnected")
    assert events.index("writer_stopped") < events.index("db_disconnected")
    assert events.index("client_closed") < events.index("redis_disconnected")
