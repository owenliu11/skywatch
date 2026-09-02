"""SkyWatch FastAPI application."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import REGIONS
from app.db import check_db, connect_db, disconnect_db
from app.ingest.opensky import FetchResult, OpenSkyClient, states_budget
from app.ingest.scheduler import RegionScheduler
from app.models import StatesSnapshot
from app.redis_client import (
    check_redis,
    connect_redis,
    disconnect_redis,
)

log = logging.getLogger(__name__)
latest_snapshots: dict[str, StatesSnapshot] = {}

async def handle_ingest_result(result: FetchResult) -> None:
    """Temporary Phase 1 ingestion sink.

    Phase 2 will persist these snapshots to PostgreSQL.
    """

    if not result.ok or result.parsed is None:
        return

    snapshot = result.parsed.snapshot
    latest_snapshots[result.region] = snapshot

    log.info(
        "ingest region=%s aircraft=%d parse_errors=%d credits=%s",
        result.region,
        snapshot.aircraft_count,
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

    # Phase 0 infrastructure.
    await connect_db()
    await connect_redis()

    log.info("Database and Redis connected")

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

        await disconnect_redis()
        await disconnect_db()

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
async def health() -> dict[str, object]:
    """Return SkyWatch dependency health."""

    db_ok = await check_db()
    redis_ok = await check_redis()

    status = "ok" if db_ok and redis_ok else "degraded"

    return {
        "status": status,
        "database": db_ok,
        "redis": redis_ok,
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