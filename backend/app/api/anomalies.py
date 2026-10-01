"""Bounded union of durable, pending, and open anomaly events."""

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request

from app.db import pool as db_pool

router = APIRouter()


@router.get("/anomalies")
async def get_anomalies(
    request: Request,
    since: datetime | None = None,
    until: datetime | None = None,
    anomaly_type: str | None = Query(None, alias="type"),
    icao24: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    include_synthetic: bool = False,
) -> dict[str, object]:
    until = until or datetime.now(UTC)
    since = since or until - timedelta(hours=1)
    if since.tzinfo is None or until.tzinfo is None or since >= until:
        raise HTTPException(422, "Use timezone-aware bounds with since < until")
    lower = max(since, until - timedelta(hours=24))
    aircraft_id = icao24.lower() if icao24 else None
    detector = anomaly_type
    if detector and not detector.startswith("rule:"):
        detector = "rule:" + detector
    # Snapshot before awaiting the DB: a concurrent flush cannot make an event disappear.
    pipeline = getattr(request.app.state, "detector", None)
    memory = pipeline.store.rows() if pipeline else []
    async with db_pool().acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT event_id,icao24,detector,window_start,window_end,hit_count,score,
                   features,region,detected_at,is_synthetic
            FROM anomalies
            WHERE window_start >= $1 AND window_start < $2
              AND ($3::text IS NULL OR detector=$3)
              AND ($4::text IS NULL OR icao24=$4)
              AND ($5 OR NOT is_synthetic)
            ORDER BY window_start DESC,event_id LIMIT $6
        """,
            lower,
            until,
            detector,
            aircraft_id,
            include_synthetic,
            limit + 1,
        )
    merged: dict[str, dict[str, object]] = {}
    for row in rows:
        item = dict(row)
        for key in ("window_start", "window_end", "detected_at"):
            item[key] = item[key].timestamp()
        if isinstance(item["features"], str):
            item["features"] = json.loads(item["features"])
        merged[item["event_id"]] = item
    for item in memory:
        if (
            lower.timestamp() <= item["window_start"] < until.timestamp()
            and (detector is None or item["detector"] == detector)
            and (aircraft_id is None or item["icao24"] == aircraft_id)
            and (include_synthetic or not item["is_synthetic"])
        ):
            merged[item["event_id"]] = item
    ordered = sorted(
        merged.values(),
        key=lambda r: (float(str(r["window_start"])), str(r["event_id"])),
        reverse=True,
    )
    return {
        "since": lower.isoformat(),
        "until": until.isoformat(),
        "window_clamped": lower != since,
        "truncated": len(ordered) > limit,
        "count": min(len(ordered), limit),
        "anomalies": ordered[:limit],
    }
