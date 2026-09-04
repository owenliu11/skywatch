"""SkyWatch FastAPI application."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException

from app.config import REGIONS
from app.db import (
    check_db,
    connect_db,
    disconnect_db,
)
from app.db import (
    pool as db_pool,
)
from app.ingest.opensky import FetchResult, OpenSkyClient, states_budget
from app.ingest.scheduler import RegionScheduler
from app.ingest.writer import (
    QueuedVector,
    metrics as writer_metrics,
    offer_vector,
    queue as write_queue,
    queue_depth,
    queue_max,
    writer_loop,
)
from app.migrate import apply_migrations
from app.models import StatesSnapshot
from app.redis_client import (
    check_redis,
    connect_redis,
    disconnect_redis,
)

log = logging.getLogger(__name__)
latest_snapshots: dict[str, StatesSnapshot] = {}

async def handle_ingest_result(result: FetchResult) -> None:
    """
    Handle one successful OpenSky region result.

    The latest snapshot is kept in memory for the future live API,
    while each StateVector is independently offered to the
    database write queue.
    """

    if not result.ok or result.parsed is None:
        return

    snapshot = result.parsed.snapshot

    # Current-state path.
    #
    # This is deliberately independent of the database queue.
    # Phase 3's live map must not depend on PostgreSQL keeping up.
    latest_snapshots[result.region] = snapshot

    # Storage provenance comes from OpenSky's response snapshot time,
    # not datetime.now().
    fetched_at = datetime.fromtimestamp(
        snapshot.time,
        tz=UTC,
    )

    accepted = 0

    for state in snapshot.states:
        queued = QueuedVector(
            state=state,
            region=snapshot.region,
            fetched_at=fetched_at,
        )

        if offer_vector(queued):
            accepted += 1

    dropped = snapshot.aircraft_count - accepted

    log.info(
        (
            "ingest region=%s aircraft=%d queued=%d dropped=%d "
            "parse_errors=%d credits=%s"
        ),
        result.region,
        snapshot.aircraft_count,
        accepted,
        dropped,
        result.parsed.error_count,
        result.credits_remaining,
    )


async def get_budget_fraction() -> float:
    """Return the fraction of the OpenSky credit budget remaining."""

    total = states_budget.daily_budget

    if total <= 0:
        return 0.0

    return states_budget.remaining() / total


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop SkyWatch dependencies and background services."""

    log.info("Starting SkyWatch")

# Phase 0 / Phase 2 infrastructure.
    await connect_db()

# A clean clone can bring itself to the current schema automatically.
    await apply_migrations(db_pool())

    await connect_redis()

    log.info("Database migrations complete; Database and Redis connected")

    # Phase 2 database writer.
    writer_task = asyncio.create_task(
    writer_loop(),
    name="skywatch-db-writer",
    )

    app.state.writer_task = writer_task

    log.info("SkyWatch database writer started")

    # Phase 1 OpenSky ingestion.
    client = OpenSkyClient()

    scheduler = RegionScheduler(
        client=client,
        regions=list(REGIONS.values()),
        budget_fraction=get_budget_fraction,
        on_result=handle_ingest_result,
    )

    app.state.opensky_client = client
    app.state.region_scheduler = scheduler

    scheduler_task = asyncio.create_task(
        scheduler.run(),
        name="skywatch-region-scheduler",
    )

    app.state.scheduler_task = scheduler_task

    log.info(
        "SkyWatch scheduler started with %d regions",
        len(REGIONS),
    )

    try:
        yield

    finally:
        log.info("Stopping SkyWatch")

        await scheduler.stop()

        if not scheduler_task.done():
            scheduler_task.cancel()

        await asyncio.gather(
            scheduler_task,
            return_exceptions=True,
        )

        # No more OpenSky records are being produced now.
        #
        # Give the database writer a chance to finish everything already
        # accepted into the queue before shutting it down.
        if not writer_task.done():
            try:
                await asyncio.wait_for(
                    write_queue.join(),
                    timeout=10.0,
                )
            except TimeoutError:
                log.warning(
                    "Timed out waiting for database queue to drain; "
                    "remaining=%d",
                    write_queue.qsize(),
                )

            writer_task.cancel()

        await asyncio.gather(
            writer_task,
            return_exceptions=True,
        )

        await disconnect_redis()
        await disconnect_db()


app = FastAPI(
    title="SkyWatch",
    version="0.1.0",
    description=(
        "Real-time airspace monitoring and anomaly detection service"
    ),
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, object]:
    """Return dependency and ingestion-pipeline health."""

    db_ok = await check_db()
    redis_ok = await check_redis()

    status = "ok" if db_ok and redis_ok else "degraded"

    deduped_total = max(
        writer_metrics.rows_offered_total
        - writer_metrics.dropped_total
        - writer_metrics.rows_inserted_total,
        0,
    )

    return {
        "status": status,
        "database": db_ok,
        "redis": redis_ok,

        # OpenSky credit budget.
        "credits_remaining": states_budget.remaining(),
        "credit_budget": states_budget.daily_budget,

        # Database queue.
        "queue_depth": queue_depth(),
        "queue_max": queue_max(),

        # State-vector flow.
        "rows_offered_total": writer_metrics.rows_offered_total,
        "rows_inserted_total": writer_metrics.rows_inserted_total,
        "rows_deduped_total": deduped_total,

        # Backpressure.
        "dropped_total": writer_metrics.dropped_total,
        "last_drop_at": (
            writer_metrics.last_drop_at.isoformat()
            if writer_metrics.last_drop_at is not None
            else None
        ),

        # Batch writer.
        "batches_written_total": (
            writer_metrics.batches_written_total
        ),
        "last_batch_rows": writer_metrics.last_batch_rows,
        "last_batch_ms": (
            round(writer_metrics.last_batch_ms, 2)
            if writer_metrics.last_batch_ms is not None
            else None
        ),
    }

@app.get("/api/aircraft")
async def get_aircraft(region: str = "bay_area") -> dict[str, object]:
    """Return the latest aircraft observed in one region."""

    if region not in REGIONS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown region: {region}",
        )

    snapshot = latest_snapshots.get(region)

    if snapshot is None:
        raise HTTPException(
            status_code=503,
            detail=f"No aircraft snapshot available yet for region: {region}",
        )

    aircraft = [
        {
            "icao24": state.icao24,
            "callsign": state.callsign,
            "origin_country": state.origin_country,
            "latitude": state.latitude,
            "longitude": state.longitude,
            "baro_altitude": state.baro_altitude,
            "geo_altitude": state.geo_altitude,
            "velocity": state.velocity,
            "true_track": state.true_track,
            "vertical_rate": state.vertical_rate,
            "on_ground": state.on_ground,
            "squawk": state.squawk,
            "category": state.category,
            "last_contact": state.last_contact,
        }
        for state in snapshot.states
    ]

    return {
        "region": region,
        "snapshot_time": snapshot.time,
        "aircraft_count": snapshot.aircraft_count,
        "aircraft": aircraft,
    }