import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from starlette.requests import Request

import app.main as main_module
from app.detect.windows import TrackWindows
from app.ingest import writer
from app.ingest.opensky import FetchResult
from app.ingest.parser import ParseResult
from app.models import StatesSnapshot, StateVector
from app.state import LiveState


@pytest.fixture(autouse=True)
def reset_state() -> None:
    while not writer.queue.empty():
        writer.queue.get_nowait()
        writer.queue.task_done()

    writer.metrics.rows_offered_total = 0
    writer.metrics.dropped_total = 0
    writer.metrics.last_drop_at = None
    writer._pending_gaps.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_full", [False, True])
async def test_ingest_result_updates_memory_and_queue(
    monkeypatch: pytest.MonkeyPatch, queue_full: bool,
) -> None:
    state = StateVector(
        icao24="abc123",
        origin_country="United States",
        last_contact=1_700_000_000,
        on_ground=False,
        spi=False,
        position_source=0,
        callsign="TEST123",
        time_position=1_700_000_000,
        longitude=-122.4,
        latitude=37.7,
        baro_altitude=10_000.0,
        velocity=200.0,
        true_track=90.0,
        vertical_rate=0.0,
        geo_altitude=10_050.0,
        squawk=None,
        sensors=None,
        category=3,
    )

    snapshot = StatesSnapshot(
        time=1_700_000_005,
        region="bay_area",
        states=(state,),
    )

    parsed = ParseResult(
        snapshot=snapshot,
        errors=(),
    )

    result = FetchResult(
        region="bay_area",
        ok=True,
        parsed=parsed,
        credits_remaining=3000,
        retry_after_s=None,
        status_code=200,
        error=None,
    )

    live = LiveState()

    windows = TrackWindows()
    events: list[str] = []

    def append(sv: StateVector, region: str) -> bool:
        assert live.snapshot()[sv.icao24].state is sv
        assert sv is state
        assert region == snapshot.region
        events.append("detector")
        return False

    monkeypatch.setattr(windows, "append", append)
    target_queue: asyncio.Queue[writer.QueuedVector] = asyncio.Queue(maxsize=1)
    if queue_full:
        target_queue.put_nowait(writer.QueuedVector(state, snapshot.region, datetime.now(UTC)))
    monkeypatch.setattr(writer, "queue", target_queue)

    def offer(item: writer.QueuedVector) -> bool:
        assert events == ["detector"]
        events.append("queue")
        return writer.offer_vector(item)

    monkeypatch.setattr(main_module, "offer_vector", offer)

    await main_module.handle_ingest_result(
        result,
        live,
        windows,
    )

    assert events == ["detector", "queue"]
    assert windows.ready is False
    if queue_full:
        assert writer.metrics.rows_offered_total == 1
        assert writer.metrics.dropped_total == 1
        assert writer.pending_gap_count() == 1
        assert live.snapshot()[state.icao24].state is state
        target_queue.get_nowait()
        target_queue.task_done()
        return

    # Phase 3: verify the current-state map was updated.
    current = live.snapshot()

    assert len(current) == 1
    assert "abc123" in current

    record = current["abc123"]

    assert record.state == state
    assert record.region == "bay_area"
    assert record.received_at.tzinfo == UTC

    # Phase 2 storage path must still receive the same vector.
    assert writer.queue.qsize() == 1

    item = writer.queue.get_nowait()

    assert item.state == state
    assert item.region == "bay_area"

    assert item.fetched_at == datetime.fromtimestamp(
        snapshot.time,
        tz=UTC,
    )

    writer.queue.task_done()

    assert writer.metrics.rows_offered_total == 1
    assert writer.metrics.dropped_total == 0


@pytest.mark.asyncio
async def test_failed_ingest_does_not_reach_detector_or_queue(monkeypatch) -> None:
    windows = Mock(spec=TrackWindows)
    offer = Mock()
    monkeypatch.setattr(main_module, "offer_vector", offer)
    result = FetchResult(
        region="bay_area", ok=False, parsed=None, credits_remaining=None,
        retry_after_s=None, status_code=503, error="unavailable",
    )
    live = LiveState()
    await main_module.handle_ingest_result(result, live, windows)
    windows.append.assert_not_called()
    offer.assert_not_called()
    assert len(live) == 0


def test_empty_windows_are_not_ready() -> None:
    windows = TrackWindows()
    assert windows.ready is False
    assert len(windows) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("initialized", [False, True])
async def test_health_reports_detector_not_ready(monkeypatch, initialized) -> None:
    app = FastAPI()
    app.state.live = LiveState()
    if initialized:
        app.state.windows = TrackWindows()
    monkeypatch.setattr(main_module, "check_db", AsyncMock(return_value=True))
    monkeypatch.setattr(main_module, "check_redis", AsyncMock(return_value=True))
    request = Request({"type": "http", "app": app})
    health = await main_module.health(request)
    assert health["detector_ready"] == 0
    assert health["status"] == "ok"


@pytest.mark.asyncio
async def test_positionless_vector_reaches_history_but_not_detection(monkeypatch):
    state = StateVector(
        icao24='abc123', origin_country='United States', last_contact=1_800_000_000,
        on_ground=False, spi=False, position_source=0, callsign=None, time_position=None,
        latitude=None, longitude=None, baro_altitude=None, geo_altitude=None,
        velocity=None, true_track=None, vertical_rate=None, squawk=None, sensors=None,
    )
    snapshot = StatesSnapshot(time=1_800_000_001,region='bay_area',states=(state,))
    result = FetchResult(region='bay_area',ok=True,
        parsed=ParseResult(snapshot=snapshot,errors=()),credits_remaining=None,
        retry_after_s=None,status_code=200,error=None)
    target = asyncio.Queue(maxsize=1)
    monkeypatch.setattr(writer,'queue',target)
    windows = TrackWindows()
    await main_module.handle_ingest_result(result,LiveState(),windows)
    assert len(windows) == 0
    assert target.get_nowait().state is state
    target.task_done()
