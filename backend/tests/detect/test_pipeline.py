from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.api import anomalies as api
from app.api.ws import FIELDS, Connection, offer_frame
from app.detect.features import FeatureComputer
from app.detect.gaps import RecentGaps
from app.detect.pipeline import DetectionPipeline
from app.detect.store import AnomalyStore
from app.detect.windows import Sample, TrackWindows


def features():
    now = datetime.now(UTC).timestamp() - 100
    samples = tuple(
        Sample(now + i * 10, now + i * 10, 0, i * 0.02, 10000, 10050, 200, 90, 0, False, "bay_area")
        for i in range(10)
    )
    f = FeatureComputer().compute("abc123", samples)
    assert f is not None
    return replace(f, jump_speed_ms=1000)


@pytest.mark.asyncio
async def test_merge_flush_retry_and_endpoint(db_connection, monkeypatch):
    f = features()
    store = AnomalyStore()
    opened = store.hit("rule:teleport", f, f.window_end)
    assert opened is not None
    assert store.hit("rule:teleport", f, f.window_end) is None
    f2 = replace(f, window_end=f.window_end + 10)
    assert store.hit("rule:teleport", f2, f2.window_end) is None
    assert opened.hit_count == 2
    store.close_expired(f2.window_end + 120)
    assert len(store.open) == 1
    store.close_expired(f2.window_end + 121)
    assert len(store.pending) == 1
    await store.flush(db_connection)
    await store.flush(db_connection)
    row = await db_connection.fetchrow("SELECT * FROM anomalies WHERE event_id=$1", opened.event_id)
    assert row["hit_count"] == 2
    assert row["window_end"].timestamp() == pytest.approx(f2.window_end)
    assert store.flushed_total == 1
    # Retry after an ambiguous commit uses the same id and cannot duplicate a row.
    store.pending[opened.event_id] = opened
    await store.flush(db_connection)
    assert (
        await db_connection.fetchval(
            "SELECT count(*) FROM anomalies WHERE event_id=$1", opened.event_id
        )
        == 1
    )
    second = store.hit(
        "rule:impossible_turn", replace(f, window_start=f.window_start + 1), f.window_end
    )
    assert second is not None
    synthetic = store.hit(
        "rule:speed_mismatch", replace(f, window_start=f.window_start + 2), f.window_end
    )
    assert synthetic is not None
    synthetic.is_synthetic = True

    class Acquire:
        async def __aenter__(self):
            return db_connection

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(api, "db_pool", lambda: SimpleNamespace(acquire=Acquire))
    app = FastAPI()
    app.state.detector = SimpleNamespace(store=store)
    request = Request({"type": "http", "app": app})
    until = datetime.now(UTC) + timedelta(seconds=10)
    result = await api.get_anomalies(
        request, until - timedelta(days=2), until, None, "ABC123", 500, False
    )
    assert result["window_clamped"]
    assert result["count"] == 2
    result = await api.get_anomalies(request, None, until, None, "abc123", 1, False)
    assert result["truncated"]
    result = await api.get_anomalies(request, None, until, "teleport", "abc123", 500, False)
    assert result["count"] == 1
    result = await api.get_anomalies(request, None, until, None, "abc123", 500, True)
    assert result["count"] == 3


def test_tick_scores_once_evicts_and_separate_frame_drop():
    windows = TrackWindows()
    for i in range(5):
        windows.append_sample(
            "abc123",
            Sample(i * 10, i * 10, 0, i * 0.5, 10000, 10050, 200, 90, 0, False, "bay_area"),
        )
    pipeline = DetectionPipeline(windows, RecentGaps())
    frames = pipeline.tick(40, -20)
    assert len(frames) == 1
    assert pipeline.tick(41, -19) == []
    assert pipeline.features.computed == 1
    c = Connection(Mock())
    original = FIELDS.copy()
    for _ in range(c.out.maxsize):
        assert offer_frame(c, {"t": "delta"})
    assert not offer_frame(c, {"t": "anomaly", "anomalies": frames})
    assert c.needs_snapshot and not c.known
    assert FIELDS == original
    pipeline.tick(200, 140)
    assert len(windows) == 0
    assert not pipeline.scored_at
    assert len(pipeline.store.pending) == 1


@pytest.mark.asyncio
async def test_failed_insert_retains_pending_event():
    from unittest.mock import AsyncMock

    store = AnomalyStore()
    f = features()
    event = store.hit("rule:teleport", f, f.window_end)
    store.close_expired(f.window_end + 121)
    connection = SimpleNamespace(execute=AsyncMock(side_effect=OSError("offline")))
    with pytest.raises(OSError):
        await store.flush(connection)
    assert event is not None and store.pending[event.event_id] is event
    assert store.flushed_total == 0
