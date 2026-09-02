import asyncio

import pytest
from fastapi import FastAPI

import app.main as main_module


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def fake_connect_db() -> None:
        events.append("db_connected")

    async def fake_connect_redis() -> None:
        events.append("redis_connected")

    async def fake_disconnect_db() -> None:
        events.append("db_disconnected")

    async def fake_disconnect_redis() -> None:
        events.append("redis_disconnected")

    class FakeScheduler:
        def __init__(self, **kwargs) -> None:
            self.run_started = asyncio.Event()
            self.stop_requested = asyncio.Event()

        async def run(self) -> None:
            events.append("scheduler_started")
            self.run_started.set()

            # Behave like the real scheduler: remain alive until stopped.
            await self.stop_requested.wait()

            events.append("scheduler_run_finished")

        async def stop(self) -> None:
            events.append("scheduler_stopped")
            self.stop_requested.set()
            # Give run() a chance to observe the stop request and exit.
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

        assert test_app.state.opensky_client is not None
        assert test_app.state.region_scheduler is scheduler
        assert test_app.state.scheduler_task is not None

        assert "db_connected" in events
        assert "redis_connected" in events
        assert "scheduler_started" in events

        # Shutdown hasn't happened yet.
        assert "db_disconnected" not in events
        assert "redis_disconnected" not in events

    assert "scheduler_stopped" in events
    assert "scheduler_run_finished" in events
    assert "redis_disconnected" in events
    assert "db_disconnected" in events

    # Infrastructure must connect before the scheduler starts.
    assert events.index("db_connected") < events.index(
        "scheduler_started"
    )
    assert events.index("redis_connected") < events.index(
        "scheduler_started"
    )

    # Scheduler must stop before infrastructure is disconnected.
    assert events.index("scheduler_stopped") < events.index(
        "redis_disconnected"
    )
    assert events.index("scheduler_stopped") < events.index(
        "db_disconnected"
    )