"""Bounded queue -> batched upsert.

The queue feeds *only* the database. The in-memory current-state map that
Phase 3's WebSocket reads is updated on the ingest path directly, so a queue
drop costs history, never liveness. That is why the backpressure policy here
is drop-newest:

- Block ingestion -- wrong. The poller runs on a credit budget and a cadence;
  blocking stalls a response we already paid for and distorts the scheduler's
  interval arithmetic.
- Drop oldest -- puts the gap in the middle of already-written history, silently.
- Drop newest -- the gap is at a known boundary, at a known time, and the write
  path fails at exactly one place. This one.

Every dropped run is recorded in `ingest_gaps` so Phase 4 can join against it
and tell a real gap in the sky from one we created.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import NamedTuple

import asyncpg

from app.db import pool
from app.models import StateVector

log = logging.getLogger(__name__)

QUEUE_MAXSIZE = 10_000
BATCH_MAX = 1000
BATCH_TIMEOUT_S = 0.5

# One retry on a transient DB error before the batch is written off as a gap.
_DB_RETRY_SLEEP_S = 0.5
# Cap the in-memory gap backlog if the DB stays down for a long stretch.
_MAX_PENDING_GAPS = 1000

_STATE_COLUMNS = (
    "time",
    "icao24",
    "time_position",
    "latitude",
    "longitude",
    "baro_altitude",
    "geo_altitude",
    "velocity",
    "true_track",
    "vertical_rate",
    "on_ground",
    "squawk",
    "spi",
    "position_source",
    "region",
)

_STATE_INSERT_SQL = """
INSERT INTO state_vectors (
    time, icao24, time_position, latitude, longitude,
    baro_altitude, geo_altitude, velocity, true_track, vertical_rate,
    on_ground, squawk, spi, position_source, region
)
SELECT * FROM unnest(
    $1::timestamptz[], $2::text[], $3::timestamptz[],
    $4::double precision[], $5::double precision[],
    $6::real[], $7::real[], $8::real[], $9::real[], $10::real[],
    $11::boolean[], $12::text[], $13::boolean[], $14::smallint[], $15::text[]
)
ON CONFLICT (icao24, time) DO NOTHING
"""

_AIRCRAFT_UPSERT_SQL = """
INSERT INTO aircraft (icao24, callsign, origin_country, category, first_seen, last_seen)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (icao24) DO UPDATE SET
    callsign       = COALESCE(EXCLUDED.callsign,       aircraft.callsign),
    origin_country = COALESCE(EXCLUDED.origin_country, aircraft.origin_country),
    category       = COALESCE(EXCLUDED.category,       aircraft.category),
    first_seen     = LEAST(aircraft.first_seen,    EXCLUDED.first_seen),
    last_seen      = GREATEST(aircraft.last_seen,   EXCLUDED.last_seen)
"""


class QueuedVector(NamedTuple):
    """A parsed state vector plus the provenance the frozen model omits."""

    state: StateVector
    region: str
    fetched_at: datetime  # snapshot time from the response body, not now()


@dataclass
class _Gap:
    region: str
    started_at: datetime
    ended_at: datetime
    dropped_count: int
    reason: str  # 'queue_full' | 'db_unavailable'


def _epoch_to_dt(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def _parse_insert_count(status: str) -> int:
    """`INSERT 0 5` -> 5. Anything unexpected -> 0."""
    parts = status.split()
    if len(parts) == 3 and parts[0] == "INSERT":
        try:
            return int(parts[2])
        except ValueError:
            return 0
    return 0


class StateWriter:
    """Drains QueuedVectors into TimescaleDB in batches."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[QueuedVector] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)

        self.rows_offered_total = 0
        self.rows_inserted_total = 0
        self.dropped_total = 0
        self.last_drop_at: datetime | None = None
        self.batches_written_total = 0
        self.last_batch_rows = 0
        self.last_batch_ms = 0.0
        self.db_ok = True

        # Drop burst currently being accumulated, plus gaps waiting for a
        # working DB connection to be written to `ingest_gaps`.
        self._open_drop_gap: _Gap | None = None
        self._pending_gaps: list[_Gap] = []

    # -- ingest path -----------------------------------------------------------

    def offer(self, item: QueuedVector) -> bool:
        """Enqueue without blocking. Returns False if the row was dropped."""

        self.rows_offered_total += 1

        try:
            self.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self._record_drop(item.region)
            return False

    def _record_drop(self, region: str) -> None:
        now = datetime.now(tz=UTC)
        self.dropped_total += 1
        self.last_drop_at = now

        if self._open_drop_gap is None:
            self._open_drop_gap = _Gap(
                region=region,
                started_at=now,
                ended_at=now,
                dropped_count=1,
                reason="queue_full",
            )
        else:
            self._open_drop_gap.ended_at = now
            self._open_drop_gap.dropped_count += 1
            if region != self._open_drop_gap.region:
                self._open_drop_gap.region = "mixed"

    # -- writer task ---------------------------------------------------------

    async def run(self) -> None:
        """Batch-drain the queue until cancelled, then drain what's left."""

        try:
            while True:
                batch = await self._collect_batch()
                if batch:
                    await self._write_batch(batch)
        except asyncio.CancelledError:
            await self._final_drain()
            raise

    async def _collect_batch(self) -> list[QueuedVector]:
        first = await self.queue.get()
        batch = [first]

        # Flush after BATCH_TIMEOUT_S even if BATCH_MAX isn't reached, or a
        # sparse region at 4am sits in the queue indefinitely. Cancelling a
        # pending Queue.get() does not lose an item.
        try:
            async with asyncio.timeout(BATCH_TIMEOUT_S):
                while len(batch) < BATCH_MAX:
                    batch.append(await self.queue.get())
        except TimeoutError:
            pass

        return batch

    async def _final_drain(self) -> None:
        leftovers: list[QueuedVector] = []
        while True:
            try:
                leftovers.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        for start in range(0, len(leftovers), BATCH_MAX):
            chunk = leftovers[start : start + BATCH_MAX]
            try:
                await self._write_batch(chunk)
            except Exception:
                # Best effort on shutdown: log and keep draining the rest.
                log.exception(
                    "final drain: batch write failed, %d rows lost", len(chunk)
                )

    # -- write path ---------------------------------------------------------

    async def _write_batch(self, batch: list[QueuedVector]) -> None:
        # A queue-full burst that has stopped arriving is now history.
        if self._open_drop_gap is not None:
            self._enqueue_gap(self._open_drop_gap)
            self._open_drop_gap = None

        state_rows = _dedupe_states(batch)
        aircraft_rows = _collapse_aircraft(batch)
        columns = list(zip(*state_rows, strict=True)) if state_rows else None

        started = perf_counter()

        for attempt in (1, 2):
            try:
                async with pool().acquire() as conn:
                    if columns is not None:
                        async with conn.transaction():
                            status = await conn.execute(_STATE_INSERT_SQL, *columns)
                            await conn.executemany(_AIRCRAFT_UPSERT_SQL, aircraft_rows)
                        self.rows_inserted_total += _parse_insert_count(status)

                    self.batches_written_total += 1
                    self.last_batch_rows = len(state_rows)
                    self.last_batch_ms = (perf_counter() - started) * 1000.0
                    self.db_ok = True

                    await self._flush_pending_gaps(conn)
                return

            except (asyncpg.PostgresError, OSError) as exc:
                if attempt == 1:
                    log.warning("batch write failed, retrying once: %s", exc)
                    await asyncio.sleep(_DB_RETRY_SLEEP_S)
                    continue

                self.db_ok = False
                log.error(
                    "batch write failed after retry; recording a %d-row gap: %s",
                    len(batch),
                    exc,
                )
                times = [_epoch_to_dt(q.state.last_contact) for q in batch]
                stamps = [t for t in times if t is not None]
                self._enqueue_gap(
                    _Gap(
                        region=_batch_region(batch),
                        started_at=min(stamps) if stamps else datetime.now(tz=UTC),
                        ended_at=max(stamps) if stamps else datetime.now(tz=UTC),
                        dropped_count=len(batch),
                        reason="db_unavailable",
                    )
                )

    def _enqueue_gap(self, gap: _Gap) -> None:
        self._pending_gaps.append(gap)
        if len(self._pending_gaps) > _MAX_PENDING_GAPS:
            self._pending_gaps.pop(0)

    async def _flush_pending_gaps(self, conn: asyncpg.Connection) -> None:
        if not self._pending_gaps:
            return

        rows = [
            (g.region, g.started_at, g.ended_at, g.dropped_count, g.reason)
            for g in self._pending_gaps
        ]
        try:
            await conn.executemany(
                "INSERT INTO ingest_gaps "
                "(region, started_at, ended_at, dropped_count, reason) "
                "VALUES ($1, $2, $3, $4, $5)",
                rows,
            )
            self._pending_gaps.clear()
        except (asyncpg.PostgresError, OSError) as exc:
            log.warning("could not flush %d ingest_gaps rows: %s", len(rows), exc)

    # -- health ------------------------------------------------------------

    def metrics(self) -> dict[str, object]:
        return {
            "queue_depth": self.queue.qsize(),
            "queue_max": self.queue.maxsize,
            "rows_offered_total": self.rows_offered_total,
            "rows_inserted_total": self.rows_inserted_total,
            "dropped_total": self.dropped_total,
            "last_drop_at": (
                self.last_drop_at.isoformat() if self.last_drop_at else None
            ),
            "batches_written_total": self.batches_written_total,
            "last_batch_rows": self.last_batch_rows,
            "last_batch_ms": round(self.last_batch_ms, 1),
            "db_ok": self.db_ok,
        }


def _state_row(item: QueuedVector) -> tuple[object, ...]:
    s = item.state
    return (
        _epoch_to_dt(s.last_contact),
        s.icao24,
        _epoch_to_dt(s.time_position),
        s.latitude,
        s.longitude,
        s.baro_altitude,
        s.geo_altitude,
        s.velocity,
        s.true_track,
        s.vertical_rate,
        s.on_ground,
        s.squawk,
        s.spi,
        s.position_source,
        item.region,
    )


def _dedupe_states(batch: list[QueuedVector]) -> list[tuple[object, ...]]:
    """One row per (icao24, time); last occurrence wins.

    ON CONFLICT covers rows already in the table; this covers duplicates
    inside the same batch so the inserted-count metric stays honest.
    """
    seen: dict[tuple[str, object], tuple[object, ...]] = {}
    for item in batch:
        row = _state_row(item)
        seen[(item.state.icao24, row[0])] = row
    return list(seen.values())


@dataclass
class _AircraftAcc:
    callsign: str | None
    origin_country: str | None
    category: int | None
    first_seen: datetime
    last_seen: datetime


def _collapse_aircraft(
    batch: list[QueuedVector],
) -> list[tuple[str, str | None, str | None, int | None, datetime, datetime]]:
    """One aircraft-dimension row per icao24 for the batch."""
    acc: dict[str, _AircraftAcc] = {}
    for item in batch:
        s = item.state
        cur = acc.get(s.icao24)
        if cur is None:
            acc[s.icao24] = _AircraftAcc(
                callsign=s.callsign,
                origin_country=s.origin_country,
                category=s.category,
                first_seen=item.fetched_at,
                last_seen=item.fetched_at,
            )
            continue
        if s.callsign is not None:
            cur.callsign = s.callsign
        if s.origin_country is not None:
            cur.origin_country = s.origin_country
        if s.category is not None:
            cur.category = s.category
        cur.first_seen = min(cur.first_seen, item.fetched_at)
        cur.last_seen = max(cur.last_seen, item.fetched_at)

    return [
        (
            icao24,
            v.callsign,
            v.origin_country,
            v.category,
            v.first_seen,
            v.last_seen,
        )
        for icao24, v in acc.items()
    ]


def _batch_region(batch: list[QueuedVector]) -> str:
    regions = {item.region for item in batch}
    if len(regions) == 1:
        return next(iter(regions))
    return "mixed"
