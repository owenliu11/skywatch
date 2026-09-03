"""Phase 2 storage tests against a real TimescaleDB.

The single most valuable test here is `test_nulls_survive_round_trip`: the
Phase 1 no-fabricated-zeros principle, enforced at the storage layer.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.ingest.writer import QUEUE_MAXSIZE, QueuedVector, StateWriter
from app.models import StateVector

T0 = datetime(2023, 11, 14, 22, 13, 0, tzinfo=UTC)
T1 = datetime(2023, 11, 14, 22, 14, 0, tzinfo=UTC)
T2 = datetime(2023, 11, 14, 22, 15, 0, tzinfo=UTC)


def make_state(
    *,
    icao24: str = "abc123",
    last_contact: int = 1_700_000_000,
    callsign: str | None = "UAL123",
    latitude: float | None = 37.6,
    longitude: float | None = -122.3,
    baro_altitude: float | None = 10_000.0,
    velocity: float | None = 250.0,
    true_track: float | None = 180.0,
    vertical_rate: float | None = -2.5,
    geo_altitude: float | None = 10_200.0,
    time_position: int | None = 1_700_000_000,
    category: int | None = None,
) -> StateVector:
    return StateVector(
        icao24=icao24,
        origin_country="United States",
        last_contact=last_contact,
        on_ground=False,
        spi=False,
        position_source=0,
        callsign=callsign,
        time_position=time_position,
        longitude=longitude,
        latitude=latitude,
        baro_altitude=baro_altitude,
        velocity=velocity,
        true_track=true_track,
        vertical_rate=vertical_rate,
        geo_altitude=geo_altitude,
        squawk="1200",
        sensors=None,
        category=category,
    )


def qv(
    state: StateVector,
    *,
    region: str = "bay_area",
    fetched_at: datetime = T0,
) -> QueuedVector:
    return QueuedVector(state=state, region=region, fetched_at=fetched_at)


@pytest.mark.db
async def test_dedup_same_icao24_time_writes_one_row(db_conn, writer_pool) -> None:
    writer = StateWriter()
    state = make_state()

    await writer._write_batch([qv(state)])
    await writer._write_batch([qv(state)])  # second batch hits ON CONFLICT

    count = await db_conn.fetchval(
        "SELECT count(*) FROM state_vectors WHERE icao24 = $1", state.icao24
    )
    assert count == 1
    assert writer.rows_inserted_total == 1


@pytest.mark.db
async def test_nulls_survive_round_trip(db_conn, writer_pool) -> None:
    writer = StateWriter()
    state = make_state(
        latitude=None,
        longitude=None,
        baro_altitude=None,
        geo_altitude=None,
        velocity=None,
        true_track=None,
        vertical_rate=None,
        time_position=None,
    )

    await writer._write_batch([qv(state)])

    row = await db_conn.fetchrow(
        "SELECT latitude, baro_altitude, velocity, time_position, "
        "position IS NULL AS pos_is_null "
        "FROM state_vectors WHERE icao24 = $1",
        state.icao24,
    )
    assert row["latitude"] is None
    assert row["baro_altitude"] is None
    assert row["velocity"] is None
    assert row["time_position"] is None
    assert row["pos_is_null"] is True


@pytest.mark.db
async def test_aircraft_upsert_keeps_known_callsign(db_conn, writer_pool) -> None:
    writer = StateWriter()

    await writer._write_batch(
        [qv(make_state(callsign="UAL123", last_contact=1_700_000_000))]
    )
    await writer._write_batch(
        [qv(make_state(callsign=None, last_contact=1_700_000_060))]
    )

    callsign = await db_conn.fetchval(
        "SELECT callsign FROM aircraft WHERE icao24 = $1", "abc123"
    )
    assert callsign == "UAL123"


@pytest.mark.db
async def test_first_seen_and_last_seen_are_monotonic(db_conn, writer_pool) -> None:
    writer = StateWriter()

    await writer._write_batch(
        [qv(make_state(last_contact=1_700_000_060), fetched_at=T1)]
    )
    await writer._write_batch(
        [qv(make_state(last_contact=1_700_000_120), fetched_at=T2)]
    )
    # An out-of-order later poll reporting an earlier fetch time.
    await writer._write_batch(
        [qv(make_state(last_contact=1_700_000_000), fetched_at=T0)]
    )

    row = await db_conn.fetchrow(
        "SELECT first_seen, last_seen FROM aircraft WHERE icao24 = $1", "abc123"
    )
    assert row["first_seen"] == T0
    assert row["last_seen"] == T2


def test_offer_drops_when_full_without_raising() -> None:
    writer = StateWriter()

    for i in range(QUEUE_MAXSIZE):
        assert writer.offer(qv(make_state(icao24=f"{i:06x}"))) is True

    assert writer.offer(qv(make_state(icao24="ffffff"))) is False
    assert writer.dropped_total == 1
    assert writer.last_drop_at is not None


async def test_collect_batch_flushes_on_timeout() -> None:
    writer = StateWriter()
    writer.offer(qv(make_state()))

    batch = await asyncio.wait_for(writer._collect_batch(), timeout=2.0)

    assert len(batch) == 1


@pytest.mark.db
async def test_queue_full_burst_recorded_in_ingest_gaps(db_conn, writer_pool) -> None:
    writer = StateWriter()

    writer._record_drop("bay_area")
    writer._record_drop("bay_area")
    assert writer._open_drop_gap is not None

    # A successful batch closes the drop window and flushes it.
    await writer._write_batch([qv(make_state())])

    rows = await db_conn.fetch(
        "SELECT region, dropped_count, reason FROM ingest_gaps"
    )
    assert len(rows) == 1
    assert rows[0]["region"] == "bay_area"
    assert rows[0]["dropped_count"] == 2
    assert rows[0]["reason"] == "queue_full"
    assert writer._open_drop_gap is None
