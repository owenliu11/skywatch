import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import NamedTuple

import asyncpg

from app.db import pool as db_pool
from app.models import StateVector

log = logging.getLogger(__name__)

QUEUE_MAX = 10_000
BATCH_MAX = 1_000
BATCH_TIMEOUT_S = 0.5


class QueuedVector(NamedTuple):
    """
    A StateVector plus ingestion metadata needed by storage.

    fetched_at is the OpenSky response snapshot time,
    not the local time when the item was enqueued.
    """

    state: StateVector
    region: str
    fetched_at: datetime


queue: asyncio.Queue[QueuedVector] = asyncio.Queue(maxsize=QUEUE_MAX)

@dataclass
class WriterMetrics:
    rows_offered_total: int = 0
    rows_inserted_total: int = 0

    dropped_total: int = 0
    last_drop_at: datetime | None = None

    batches_written_total: int = 0
    last_batch_rows: int = 0
    last_batch_ms: float | None = None


@dataclass
class PendingGap:
    region: str
    started_at: datetime
    ended_at: datetime
    dropped_count: int
    reason: str


metrics = WriterMetrics()

_pending_gaps: dict[tuple[str, str], PendingGap] = {}

STATE_VECTOR_INSERT = """
INSERT INTO state_vectors (
    time,
    icao24,
    time_position,
    latitude,
    longitude,
    baro_altitude,
    geo_altitude,
    velocity,
    true_track,
    vertical_rate,
    on_ground,
    squawk,
    spi,
    position_source,
    region
)
VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10,
    $11, $12, $13, $14, $15
)
ON CONFLICT (icao24, time) DO NOTHING;
"""


AIRCRAFT_UPSERT = """
INSERT INTO aircraft (
    icao24,
    callsign,
    origin_country,
    category,
    first_seen,
    last_seen
)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (icao24) DO UPDATE SET
    callsign = COALESCE(
        EXCLUDED.callsign,
        aircraft.callsign
    ),
    origin_country = COALESCE(
        EXCLUDED.origin_country,
        aircraft.origin_country
    ),
    category = COALESCE(
        EXCLUDED.category,
        aircraft.category
    ),
    first_seen = LEAST(
        aircraft.first_seen,
        EXCLUDED.first_seen
    ),
    last_seen = GREATEST(
        aircraft.last_seen,
        EXCLUDED.last_seen
    );
"""

INGEST_GAP_INSERT = """
INSERT INTO ingest_gaps (
    region,
    started_at,
    ended_at,
    dropped_count,
    reason
)
VALUES ($1, $2, $3, $4, $5);
"""

def queue_depth() -> int:
    """Return the current number of queued items."""
    return queue.qsize()


def queue_max() -> int:
    """Return the configured queue capacity."""
    return queue.maxsize

def _merge_pending_gap(gap: PendingGap) -> None:
    """
    Merge a gap into the in-memory accumulator.

    We aggregate drops by region and reason so thousands of dropped
    vectors do not create thousands of ingest_gaps rows.
    """
    key = (gap.region, gap.reason)
    current = _pending_gaps.get(key)

    if current is None:
        _pending_gaps[key] = gap
        return

    current.started_at = min(current.started_at, gap.started_at)
    current.ended_at = max(current.ended_at, gap.ended_at)
    current.dropped_count += gap.dropped_count


def _record_drop(item: QueuedVector, reason: str) -> None:
    """Record one dropped vector as part of an ingestion gap."""
    gap = PendingGap(
        region=item.region,
        started_at=item.fetched_at,
        ended_at=item.fetched_at,
        dropped_count=1,
        reason=reason,
    )

    _merge_pending_gap(gap)


def offer_vector(
    item: QueuedVector,
    target_queue: asyncio.Queue[QueuedVector] | None = None,
) -> bool:
    """
    Offer a vector to the database queue without blocking ingestion.

    Returns True when accepted.

    Returns False when the queue is full. In that case the newest
    vector is dropped and the event is recorded.
    """
    selected_queue = queue if target_queue is None else target_queue

    metrics.rows_offered_total += 1

    try:
        selected_queue.put_nowait(item)
    except asyncio.QueueFull:
        metrics.dropped_total += 1
        metrics.last_drop_at = datetime.now(UTC)

        _record_drop(
            item,
            reason="queue_full",
        )

        return False

    return True


def pending_gap_count() -> int:
    """Return the number of currently aggregated gap records."""
    return len(_pending_gaps)


def _drain_pending_gaps() -> list[PendingGap]:
    """
    Take a snapshot of pending gaps for a database write.

    New drops occurring while the database write is in progress
    will accumulate in a new dictionary entry.
    """
    gaps = list(_pending_gaps.values())
    _pending_gaps.clear()

    return gaps


def _restore_pending_gaps(gaps: list[PendingGap]) -> None:
    """
    Restore gaps if the database transaction fails.
    """
    for gap in gaps:
        _merge_pending_gap(gap)

def epoch_to_datetime(value: int | None) -> datetime | None:
    """
    Convert OpenSky epoch seconds to a timezone-aware UTC datetime.
    """
    if value is None:
        return None

    return datetime.fromtimestamp(value, tz=UTC)


def state_vector_row(item: QueuedVector) -> tuple[object, ...]:
    """
    Convert one QueuedVector into parameters for state_vectors.
    """
    state = item.state

    return (
        epoch_to_datetime(state.last_contact),
        state.icao24,
        epoch_to_datetime(state.time_position),
        state.latitude,
        state.longitude,
        state.baro_altitude,
        state.geo_altitude,
        state.velocity,
        state.true_track,
        state.vertical_rate,
        state.on_ground,
        state.squawk,
        state.spi,
        state.position_source,
        item.region,
    )

def unique_state_vector_rows(
    batch: list[QueuedVector],
) -> list[tuple[object, ...]]:
    """
    Collapse duplicate (icao24, time) keys inside one batch.

    PostgreSQL still owns the final dedup guarantee through the
    UNIQUE (icao24, time) index.
    """
    rows: list[tuple[object, ...]] = []
    seen: set[tuple[str, datetime]] = set()

    for item in batch:
        row = state_vector_row(item)

        observed_at = row[0]
        icao24 = row[1]

        if not isinstance(observed_at, datetime):
            raise TypeError("state-vector time must be datetime")

        if not isinstance(icao24, str):
            raise TypeError("state-vector ICAO24 must be str")

        key = (icao24, observed_at)

        if key in seen:
            continue

        seen.add(key)
        rows.append(row)

    return rows

async def existing_state_vector_count(
    connection: asyncpg.Connection,
    rows: list[tuple[object, ...]],
) -> int:
    """
    Count which incoming unique keys already exist.

    SkyWatch currently has one database writer task, so this gives us
    an accurate per-process inserted-row metric without scanning the
    entire hypertable.
    """
    if not rows:
        return 0

    icao24s = [row[1] for row in rows]
    times = [row[0] for row in rows]

    result = await connection.fetchval(
        """
        SELECT COUNT(*)
        FROM state_vectors AS stored
        JOIN UNNEST(
            $1::text[],
            $2::timestamptz[]
        ) AS incoming(icao24, time)
          ON stored.icao24 = incoming.icao24
         AND stored.time = incoming.time
        """,
        icao24s,
        times,
    )

    return int(result)

def aircraft_rows(
    batch: list[QueuedVector],
) -> list[tuple[object, ...]]:
    """
    Collapse a batch to one aircraft upsert per ICAO24.

    first_seen uses the earliest observation in the batch.
    last_seen uses the latest observation in the batch.

    Callsign/category come from the most recent non-null value
    seen in this batch.
    """
    grouped: dict[str, dict[str, object]] = {}

    for item in batch:
        state = item.state
        observed_at = epoch_to_datetime(state.last_contact)

        if observed_at is None:
            raise ValueError(
                f"last_contact unexpectedly missing for {state.icao24}"
            )

        current = grouped.get(state.icao24)

        if current is None:
            grouped[state.icao24] = {
                "icao24": state.icao24,
                "callsign": state.callsign,
                "origin_country": state.origin_country,
                "category": state.category,
                "first_seen": observed_at,
                "last_seen": observed_at,
                "latest_observation": observed_at,
            }
            continue

        current["first_seen"] = min(current["first_seen"], observed_at)

        current["last_seen"] = max(current["last_seen"], observed_at)

        # Only let a newer observation update descriptive fields.
        if observed_at >= current["latest_observation"]:
            if state.callsign is not None:
                current["callsign"] = state.callsign

            if state.origin_country is not None:
                current["origin_country"] = state.origin_country

            if state.category is not None:
                current["category"] = state.category

            current["latest_observation"] = observed_at

    return [
        (
            row["icao24"],
            row["callsign"],
            row["origin_country"],
            row["category"],
            row["first_seen"],
            row["last_seen"],
        )
        for row in grouped.values()
    ]


async def write_batch(
    connection: asyncpg.Connection,
    batch: list[QueuedVector],
    gaps: list[PendingGap] | None = None,
) -> int:
    """
    Write one batch and any accumulated ingestion gaps.

    Returns the number of NEW state-vector rows actually inserted.
    """
    gaps = [] if gaps is None else gaps

    if not batch and not gaps:
        return 0

    vector_rows = unique_state_vector_rows(batch)
    dimension_rows = aircraft_rows(batch)

    gap_rows = [
        (
            gap.region,
            gap.started_at,
            gap.ended_at,
            gap.dropped_count,
            gap.reason,
        )
        for gap in gaps
    ]

    async with connection.transaction():
        existing_count = await existing_state_vector_count(
            connection,
            vector_rows,
        )

        if vector_rows:
            await connection.executemany(
                STATE_VECTOR_INSERT,
                vector_rows,
            )

        if dimension_rows:
            await connection.executemany(
                AIRCRAFT_UPSERT,
                dimension_rows,
            )

        if gap_rows:
            await connection.executemany(
                INGEST_GAP_INSERT,
                gap_rows,
            )

    return len(vector_rows) - existing_count

async def collect_batch() -> list[QueuedVector]:
    """
    Collect up to BATCH_MAX items.

    After the first item arrives, flush when either:
      - BATCH_MAX is reached, or
      - BATCH_TIMEOUT_S has elapsed.
    """
    first = await queue.get()
    batch = [first]

    deadline = monotonic() + BATCH_TIMEOUT_S

    while len(batch) < BATCH_MAX:
        remaining = deadline - monotonic()

        if remaining <= 0:
            break

        try:
            item = await asyncio.wait_for(
                queue.get(),
                timeout=remaining,
            )
        except TimeoutError:
            break

        batch.append(item)

    return batch


async def writer_loop() -> None:
    """
    Continuously drain the queue and persist batches.

    Queue-full drops are aggregated and stored in ingest_gaps.
    """
    database_pool = db_pool()

    while True:
        batch = await collect_batch()
        gaps = _drain_pending_gaps()

        started = monotonic()

        try:
            async with database_pool.acquire() as connection:
                inserted_count = await write_batch(
                    connection,
                    batch,
                    gaps,
                )

        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            # History may be lost during an outage; keep live ingestion independent
            # and let the pool reconnect for the next batch after recovery.
            _restore_pending_gaps(gaps)
            metrics.dropped_total += len(batch)
            metrics.last_drop_at = datetime.now(UTC)
            for item in batch:
                _record_drop(item, reason="db_unavailable")
                queue.task_done()
            log.warning("Database write failed; dropped %d history rows", len(batch))
            await asyncio.sleep(1.0)
            continue
        except Exception:
            _restore_pending_gaps(gaps)
            raise

        else:
            elapsed_ms = (monotonic() - started) * 1000.0

            metrics.rows_inserted_total += inserted_count
            metrics.batches_written_total += 1
            metrics.last_batch_rows = len(batch)
            metrics.last_batch_ms = elapsed_ms

            for _ in batch:
                queue.task_done()