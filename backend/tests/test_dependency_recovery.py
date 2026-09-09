"""Short, deterministic dependency-outage checks without stopping shared services."""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from redis.exceptions import ConnectionError as RedisConnectionError
from test_writer import make_item

from app.config import REGIONS
from app.ingest import opensky, writer


async def test_writer_accounts_for_failed_batch_and_recovers(monkeypatch):
    queue = asyncio.Queue()
    counters = writer.WriterMetrics()
    monkeypatch.setattr(writer, "queue", queue)
    monkeypatch.setattr(writer, "metrics", counters)
    monkeypatch.setattr(writer, "_pending_gaps", {})
    calls = 0
    recovered = asyncio.Event()

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("database offline")
            yield object()

    async def write_batch(connection, batch, gaps):
        assert len(batch) == 1
        assert sum(g.dropped_count for g in gaps) == 1
        assert gaps[0].reason == "db_unavailable"
        recovered.set()
        return 1

    async def collect_batch():
        return [await queue.get()]

    monkeypatch.setattr(writer, "db_pool", Pool)
    monkeypatch.setattr(writer, "write_batch", write_batch)
    monkeypatch.setattr(writer, "collect_batch", collect_batch)
    writer.offer_vector(make_item())
    writer.offer_vector(make_item(icao24="next001"))
    task = asyncio.create_task(writer.writer_loop())
    try:
        await asyncio.wait_for(recovered.wait(), 3)
        await asyncio.wait_for(queue.join(), 1)
        assert not task.done()
        assert counters.dropped_total == 1
        assert counters.rows_inserted_total == 1
        assert counters.last_drop_at is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_redis_outage_returns_retryable_failure_then_recovers(monkeypatch):
    client = opensky.OpenSkyClient()
    token = AsyncMock(side_effect=[RedisConnectionError("offline"), "recovered-token"])
    monkeypatch.setattr(client, "get_token", token)
    import httpx
    monkeypatch.setattr(client, "_get_states", AsyncMock(return_value=httpx.Response(
        200, json={"time": 1700000000, "states": []})))
    monkeypatch.setattr(opensky, "states_budget", opensky.CreditBudget(4000))
    failed = await client.fetch_region(REGIONS["bay_area"])
    assert not failed.ok
    assert "ConnectionError" in failed.error
    assert (await client.fetch_region(REGIONS["bay_area"])).ok
