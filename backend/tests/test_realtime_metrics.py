"""Step 7 metric invariants and broadcast integration coverage."""
import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_ws import make_record

from app import main
from app.api import ws
from app.state import LiveState


@pytest.fixture
def metrics(monkeypatch):
    counters = ws.RealtimeMetrics()
    monkeypatch.setattr(ws, "metrics", counters)
    monkeypatch.setattr(main, "ws_metrics", counters)
    return counters


def test_tick_p95_is_bounded_rolling_nearest_rank(metrics):
    assert metrics.tick_ms_p95 == 0
    for value in range(1, 401):
        metrics.record_tick(value)
    assert len(metrics.tick_samples) == 300
    assert metrics.tick_ms_last == 400
    assert metrics.tick_ms_p95 == 385


@pytest.mark.parametrize("lat,lon", [(37.2, -122), (38.1, -122), (37.7, -122.6), (37.7, -121.7)])
def test_all_viewport_edges(lat, lon):
    record = make_record(latitude=lat, longitude=lon)
    viewport = ws.Viewport(37.2, -122.6, 38.1, -121.7)
    assert ws.candidates(ws.build_grid({"abc123": record}), viewport) == [record]


def test_eviction_counter_boundary_and_repeat():
    live = LiveState()
    for contact in (100, 101):
        record = make_record(icao24=str(contact), last_contact=contact)
        live.upsert(record.state, record.region)
    assert live.evict_before(datetime.fromtimestamp(101, UTC)) == 1
    assert live.evicted_total == 1
    assert live.evict_before(datetime.fromtimestamp(101, UTC)) == 0
    assert live.evicted_total == 1
    assert live.evict_before(datetime.fromtimestamp(102, UTC)) == 1
    assert live.evicted_total == 2


async def test_broadcast_drop_resync_and_idle_ticks(monkeypatch, metrics):
    live = LiveState()
    manager = ws.ConnectionManager()
    conn = manager.add(AsyncMock())
    ws.handle_client_message(conn, {"t": "viewport", "bbox": [37.2, -122.6, 38.1, -121.7]})
    record = make_record(last_contact=int(datetime.now(UTC).timestamp()))
    live.upsert(record.state, record.region)
    for _ in range(conn.out.maxsize):
        conn.out.put_nowait({"t": "delta"})
    ticks = 0

    async def next_tick(_):
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            assert conn.dropped_frames == 1
            assert metrics.frames_dropped_total == metrics.resyncs_total == 1
            assert conn.known == {} and conn.needs_snapshot
            while not conn.out.empty():
                conn.out.get_nowait()
                conn.out.task_done()
        elif ticks == 2:
            assert conn.out.get_nowait()["t"] == "snapshot"
            conn.out.task_done()
        else:
            assert conn.out.empty()  # Unchanged stationary viewport sends nothing.
            raise asyncio.CancelledError

    monkeypatch.setattr(ws.asyncio, "sleep", next_tick)
    with pytest.raises(asyncio.CancelledError):
        await ws.broadcast_loop(live, manager)
    assert len(metrics.tick_samples) == 3
    assert metrics.tick_ms_last >= 0
    assert metrics.frames_sent_total == 0  # Enqueue is not a successful send.


async def test_sender_counts_only_successful_sends(metrics):
    socket = AsyncMock()
    socket.send_json.side_effect = [None, RuntimeError("disconnected")]
    conn = ws.Connection(ws=socket)
    conn.out.put_nowait({"t": "snapshot"})
    conn.out.put_nowait({"t": "delta"})
    with pytest.raises(RuntimeError):
        await ws.sender_loop(conn)
    assert metrics.frames_sent_total == 1
    await asyncio.wait_for(conn.out.join(), 1)


def test_websocket_health_connection_lifecycle(monkeypatch, metrics):
    manager = ws.ConnectionManager(max_connections=2)
    monkeypatch.setattr(ws, "manager", manager)
    monkeypatch.setattr(main, "ws_manager", manager)
    monkeypatch.setattr(main, "check_db", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "check_redis", AsyncMock(return_value=False))
    monkeypatch.setattr(main.app.state, "live", LiveState(), raising=False)
    client = TestClient(main.app)
    with client.websocket_connect("/ws/live") as socket:
        socket.send_json({"t": "viewport", "bbox": [37.2, -122.6, 38.1, -121.7]})
        health = client.get("/health").json()
        assert health["status"] == "degraded"
        assert health["ws_connections"] == 1
        assert health["ws_connections_max"] == 2
        for key in ("live_map_size", "live_map_evicted_total", "ws_frames_sent_total",
                    "ws_frames_dropped_total", "ws_resyncs_total", "tick_ms_last", "tick_ms_p95"):
            assert health[key] == 0
        assert {"queue_depth", "queue_max", "credits_remaining", "credit_budget"} <= health.keys()
    assert client.get("/health").json()["ws_connections"] == 0
