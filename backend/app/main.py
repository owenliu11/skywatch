"""SkyWatch FastAPI application."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request

from app.api.aircraft import router as aircraft_router
from app.api.anomalies import router as anomalies_router
from app.api.tracks import router as tracks_router
from app.api.ws import (
    broadcast_loop,
)
from app.api.ws import (
    manager as ws_manager,
)
from app.api.ws import metrics as ws_metrics
from app.api.ws import (
    router as ws_router,
)
from app.config import REGIONS, settings
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
    offer_vector,
    queue_depth,
    queue_max,
    writer_loop,
)
from app.ingest.writer import (
    metrics as writer_metrics,
)
from app.ingest.writer import (
    queue as write_queue,
)
from app.migrate import apply_migrations
from app.redis_client import (
    check_redis,
    connect_redis,
    disconnect_redis,
)
from app.state import LiveState

log = logging.getLogger(__name__)


async def handle_ingest_result(
        result: FetchResult,
        live:LiveState,
    ) -> None:
    """
    Handle one successful OpenSky region result.

    The latest snapshot is kept in memory for the future live API,
    while each StateVector is independently offered to the
    database write queue.
    """

    if not result.ok or result.parsed is None:
        return

    snapshot = result.parsed.snapshot

    # Storage provenance comes from OpenSky's response snapshot time,
    # not datetime.now().
    fetched_at = datetime.fromtimestamp(
        snapshot.time,
        tz=UTC,
    )

    accepted = 0

    for state in snapshot.states:
        # Phase 3 live path.
        #
        # This must happen before the database queue. Queue backpressure
        # may cost us history, but must never cost us live state.
        live.upsert(
            state,
            snapshot.region,
        )

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
    live = LiveState()
    app.state.live = live
    broadcast_task = asyncio.create_task(
        broadcast_loop(
            live,
            ws_manager,
        ),
        name="skywatch-ws-broadcast",
    )

    app.state.broadcast_task = broadcast_task

    log.info("SkyWatch WebSocket broadcaster started")
    # Phase 2 database writer.
    writer_task = asyncio.create_task(
    writer_loop(),
    name="skywatch-db-writer",
    )

    app.state.writer_task = writer_task

    log.info("SkyWatch database writer started")

    # Phase 1 OpenSky ingestion.
    client = OpenSkyClient()

    # Adapter callback that gives the ingest handler access
    # to this process's LiveState instance.
    async def on_ingest_result(
        result: FetchResult,
    ) -> None:
        await handle_ingest_result(
            result,
            live,
        )

    enabled_region_names = [
        name.strip()
        for name in settings.poll_regions.split(",")
        if name.strip()
    ]

    enabled_regions = [
        REGIONS[name]
        for name in enabled_region_names
        if name in REGIONS
    ]

    log.info(
    "Enabled polling regions: %s",
    ", ".join(region.name for region in enabled_regions),
    )

    scheduler = RegionScheduler(
        client=client,
        regions=enabled_regions,
        budget_fraction=get_budget_fraction,
        on_result=on_ingest_result,
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
        len(enabled_regions),
    )

    try:
        yield

    finally:
        log.info("Stopping SkyWatch")
        if not broadcast_task.done():
            broadcast_task.cancel()

        await asyncio.gather(
            broadcast_task,
            return_exceptions=True,
        )
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
app.include_router(aircraft_router)
app.include_router(anomalies_router)
app.include_router(tracks_router)
app.include_router(ws_router)


@app.get("/health")
async def health(
    request: Request,
) -> dict[str, object]:
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
        # Phase 3 live state.
        "live_map_size": len(request.app.state.live),
        "live_map_evicted_total": request.app.state.live.evicted_total,
        "ws_connections": len(ws_manager),
        "ws_connections_max": ws_manager.max_connections,
        "ws_frames_sent_total": ws_metrics.frames_sent_total,
        "ws_frames_dropped_total": ws_metrics.frames_dropped_total,
        "ws_resyncs_total": ws_metrics.resyncs_total,
        "tick_ms_last": ws_metrics.tick_ms_last,
        "tick_ms_p95": ws_metrics.tick_ms_p95,
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
