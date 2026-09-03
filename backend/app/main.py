"""SkyWatch FastAPI application."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.config import REGIONS, active_regions, settings
from app.db import check_db, close_pool, init_pool, pool
from app.ingest.opensky import FetchResult, OpenSkyClient, states_budget
from app.ingest.scheduler import RegionScheduler
from app.ingest.writer import QueuedVector, StateWriter
from app.logging_config import configure_logging
from app.migrate import apply_migrations
from app.models import StatesSnapshot
from app.redis_client import (
    check_redis,
    connect_redis,
    disconnect_redis,
)

log = logging.getLogger(__name__)

latest_snapshots: dict[str, StatesSnapshot] = {}
writer = StateWriter()


async def handle_ingest_result(result: FetchResult) -> None:
    """Fan a fetch result out to the current-state map and the write queue.

    The two are deliberately decoupled: the map gives Phase 3 liveness on the
    ingest path, the queue gives the database history. A queue drop costs
    history only.
    """

    if not result.ok or result.parsed is None:
        return

    snapshot = result.parsed.snapshot
    latest_snapshots[result.region] = snapshot

    fetched_at = datetime.fromtimestamp(snapshot.time, tz=UTC)
    for state in snapshot.states:
        writer.offer(
            QueuedVector(
                state=state,
                region=result.region,
                fetched_at=fetched_at,
            )
        )

    log.info(
        "ingest region=%s aircraft=%d parse_errors=%d credits=%s "
        "queue_depth=%d dropped=%d",
        result.region,
        snapshot.aircraft_count,
        result.parsed.error_count,
        result.credits_remaining,
        writer.queue.qsize(),
        writer.dropped_total,
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

    configure_logging()
    log.info("Starting SkyWatch")

    # Phase 0 infrastructure.
    await init_pool()
    await connect_redis()
    log.info("Database and Redis connected")

    # Phase 2 schema. A clean clone plus `docker compose up` should just work.
    if settings.run_migrations_on_startup:
        await apply_migrations(pool())

    # Phase 2 write path: drain the queue into TimescaleDB.
    writer_task = asyncio.create_task(writer.run(), name="skywatch-writer")
    app.state.writer = writer
    app.state.writer_task = writer_task

    # Phase 1 OpenSky ingestion.
    client = OpenSkyClient()

    scheduler = RegionScheduler(
        client=client,
        regions=active_regions(),
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
        len(scheduler.intervals),
    )

    try:
        yield

    finally:
        log.info("Stopping SkyWatch")

        await scheduler.stop()
        if not scheduler_task.done():
            scheduler_task.cancel()
        await asyncio.gather(scheduler_task, return_exceptions=True)

        # Cancelling the writer triggers a final drain of the queue.
        writer_task.cancel()
        await asyncio.gather(writer_task, return_exceptions=True)

        await client.aclose()
        await disconnect_redis()
        await close_pool()

        log.info("SkyWatch stopped")


app = FastAPI(
    title="SkyWatch",
    version="0.1.0",
    description=(
        "Real-time airspace monitoring and anomaly detection service"
    ),
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> JSONResponse:
    """Return SkyWatch dependency health and ingestion metrics."""

    db_ok = await check_db()
    redis_ok = await check_redis()

    ok = db_ok and redis_ok
    budget_total = states_budget.daily_budget

    payload: dict[str, object] = {
        "status": "ok" if ok else "degraded",
        "database": db_ok,
        "redis": redis_ok,
        "credits_remaining": states_budget.remaining(),
        "credits_fraction": (
            round(states_budget.remaining() / budget_total, 3)
            if budget_total > 0
            else 0.0
        ),
        **writer.metrics(),
    }

    return JSONResponse(
        payload,
        status_code=200 if ok else 503,
    )


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
