import asyncio
from datetime import UTC, datetime

import asyncpg
import pytest

from app.ingest import writer
from app.models import StateVector

BASE_TIME = 1_700_000_000


def make_item(
    *,
    icao24: str = "test001",
    last_contact: int = BASE_TIME,
    region: str = "test",
    **overrides: object,
) -> writer.QueuedVector:
    """Create a valid test QueuedVector."""

    values: dict[str, object] = {
        "icao24": icao24,
        "origin_country": "United States",
        "last_contact": last_contact,
        "on_ground": False,
        "spi": False,
        "position_source": 0,
        "callsign": "TEST123",
        "time_position": last_contact,
        "longitude": -122.4,
        "latitude": 37.7,
        "baro_altitude": 10_000.0,
        "velocity": 200.0,
        "true_track": 90.0,
        "vertical_rate": 0.0,
        "geo_altitude": 10_050.0,
        "squawk": None,
        "sensors": None,
        "category": 3,
    }

    values.update(overrides)

    state = StateVector(**values)

    return writer.QueuedVector(
        state=state,
        region=region,
        fetched_at=datetime.fromtimestamp(
            last_contact,
            tz=UTC,
        ),
    )


@pytest.fixture(autouse=True)
def reset_writer_state() -> None:
    """Prevent writer metrics/gaps leaking between unit tests."""

    writer.metrics.rows_offered_total = 0
    writer.metrics.rows_inserted_total = 0

    writer.metrics.dropped_total = 0
    writer.metrics.last_drop_at = None

    writer.metrics.batches_written_total = 0
    writer.metrics.last_batch_rows = 0
    writer.metrics.last_batch_ms = None

    writer._pending_gaps.clear()


def test_epoch_to_datetime() -> None:
    result = writer.epoch_to_datetime(BASE_TIME)

    assert result == datetime.fromtimestamp(
        BASE_TIME,
        tz=UTC,
    )

    assert writer.epoch_to_datetime(None) is None


@pytest.mark.asyncio
async def test_queue_full_drops_newest() -> None:
    tiny_queue: asyncio.Queue[writer.QueuedVector] = asyncio.Queue(
        maxsize=1
    )

    item = make_item()

    assert writer.offer_vector(item, tiny_queue) is True
    assert writer.offer_vector(item, tiny_queue) is False

    assert tiny_queue.qsize() == 1

    assert writer.metrics.rows_offered_total == 2
    assert writer.metrics.dropped_total == 1
    assert writer.pending_gap_count() == 1

    gaps = writer._drain_pending_gaps()

    assert len(gaps) == 1
    assert gaps[0].reason == "queue_full"
    assert gaps[0].region == "test"
    assert gaps[0].dropped_count == 1


@pytest.mark.asyncio
async def test_batch_flushes_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_queue: asyncio.Queue[writer.QueuedVector] = asyncio.Queue(
        maxsize=10
    )

    monkeypatch.setattr(
        writer,
        "queue",
        test_queue,
    )

    # Keep the test fast while still testing timeout behavior.
    monkeypatch.setattr(
        writer,
        "BATCH_TIMEOUT_S",
        0.05,
    )

    item = make_item()

    await test_queue.put(item)

    loop = asyncio.get_running_loop()
    started = loop.time()

    batch = await writer.collect_batch()

    elapsed = loop.time() - started

    assert batch == [item]

    # It should wait for the timeout rather than flushing immediately.
    assert elapsed >= 0.04

    # But it should not hang.
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_duplicate_state_vector_is_stored_once(
    db_connection: asyncpg.Connection,
) -> None:
    item = make_item(
        icao24="testdup",
    )

    first_inserted = await writer.write_batch(
        db_connection,
        [item],
    )

    second_inserted = await writer.write_batch(
        db_connection,
        [item],
    )

    count = await db_connection.fetchval(
        """
        SELECT COUNT(*)
        FROM state_vectors
        WHERE icao24 = $1
        """,
        "testdup",
    )

    assert count == 1
    assert first_inserted == 1
    assert second_inserted == 0


@pytest.mark.asyncio
async def test_null_values_survive_database_round_trip(
    db_connection: asyncpg.Connection,
) -> None:
    item = make_item(
        icao24="testnull",
        callsign=None,
        time_position=None,
        latitude=None,
        longitude=None,
        baro_altitude=None,
        geo_altitude=None,
        velocity=None,
        true_track=None,
        vertical_rate=None,
        squawk=None,
        category=None,
    )

    await writer.write_batch(
        db_connection,
        [item],
    )

    row = await db_connection.fetchrow(
        """
        SELECT
            time_position,
            latitude,
            longitude,
            baro_altitude,
            geo_altitude,
            velocity,
            true_track,
            vertical_rate,
            position IS NULL AS pos_is_null
        FROM state_vectors
        WHERE icao24 = $1
        """,
        "testnull",
    )

    assert row is not None

    assert row["time_position"] is None
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert row["baro_altitude"] is None
    assert row["geo_altitude"] is None
    assert row["velocity"] is None
    assert row["true_track"] is None
    assert row["vertical_rate"] is None

    assert row["pos_is_null"] is True


@pytest.mark.asyncio
async def test_aircraft_upsert_preserves_known_callsign(
    db_connection: asyncpg.Connection,
) -> None:
    first = make_item(
        icao24="testcall",
        last_contact=BASE_TIME,
        callsign="UAL123",
    )

    later = make_item(
        icao24="testcall",
        last_contact=BASE_TIME + 10,
        callsign=None,
    )

    await writer.write_batch(
        db_connection,
        [first],
    )

    await writer.write_batch(
        db_connection,
        [later],
    )

    callsign = await db_connection.fetchval(
        """
        SELECT callsign
        FROM aircraft
        WHERE icao24 = $1
        """,
        "testcall",
    )

    assert callsign == "UAL123"


@pytest.mark.asyncio
async def test_first_seen_and_last_seen_are_monotonic(
    db_connection: asyncpg.Connection,
) -> None:
    latest = make_item(
        icao24="testtime",
        last_contact=BASE_TIME + 20,
    )

    earliest = make_item(
        icao24="testtime",
        last_contact=BASE_TIME,
    )

    middle = make_item(
        icao24="testtime",
        last_contact=BASE_TIME + 10,
    )

    # Deliberately write them out of chronological order.
    await writer.write_batch(
        db_connection,
        [latest],
    )

    await writer.write_batch(
        db_connection,
        [earliest],
    )

    await writer.write_batch(
        db_connection,
        [middle],
    )

    row = await db_connection.fetchrow(
        """
        SELECT first_seen, last_seen
        FROM aircraft
        WHERE icao24 = $1
        """,
        "testtime",
    )

    assert row is not None

    assert row["first_seen"] == datetime.fromtimestamp(
        BASE_TIME,
        tz=UTC,
    )

    assert row["last_seen"] == datetime.fromtimestamp(
        BASE_TIME + 20,
        tz=UTC,
    )


@pytest.mark.asyncio
async def test_ingest_gap_is_persisted(
    db_connection: asyncpg.Connection,
) -> None:
    gap_time = datetime.fromtimestamp(
        BASE_TIME,
        tz=UTC,
    )

    gap = writer.PendingGap(
        region="test_region",
        started_at=gap_time,
        ended_at=gap_time,
        dropped_count=7,
        reason="queue_full",
    )

    await writer.write_batch(
        db_connection,
        [],
        [gap],
    )

    row = await db_connection.fetchrow(
        """
        SELECT
            region,
            dropped_count,
            reason
        FROM ingest_gaps
        WHERE region = $1
        """,
        "test_region",
    )

    assert row is not None
    assert row["region"] == "test_region"
    assert row["dropped_count"] == 7
    assert row["reason"] == "queue_full"