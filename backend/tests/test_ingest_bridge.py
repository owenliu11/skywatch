from datetime import UTC, datetime

import pytest

import app.main as main_module
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
async def test_ingest_result_updates_memory_and_queue() -> None:
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

    await main_module.handle_ingest_result(
        result,
        live,
    )

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