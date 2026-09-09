"""Historical aircraft track endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

from app.db import pool as db_pool

router = APIRouter()

DEFAULT_TRACK_WINDOW = timedelta(hours=1)
MAX_TRACK_WINDOW = timedelta(hours=24)

DEFAULT_TRACK_LIMIT = 5_000
MAX_TRACK_LIMIT = 20_000


@router.get("/aircraft/{icao24}/track")
async def get_aircraft_track(
    icao24: str,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(
        DEFAULT_TRACK_LIMIT,
        ge=1,
        le=MAX_TRACK_LIMIT,
    ),
) -> dict[str, object]:
    """Return bounded historical track data from TimescaleDB."""

    now = datetime.now(UTC)

    if until is None:
        until = now
    elif until.tzinfo is None:
        raise HTTPException(
            status_code=422,
            detail="until must include a timezone",
        )
    else:
        until = until.astimezone(UTC)

    if since is None:
        since = until - DEFAULT_TRACK_WINDOW
    elif since.tzinfo is None:
        raise HTTPException(
            status_code=422,
            detail="since must include a timezone",
        )
    else:
        since = since.astimezone(UTC)

    if since >= until:
        raise HTTPException(
            status_code=422,
            detail="since must be earlier than until",
        )

    # Never allow this endpoint to scan more than 24 hours.
    minimum_since = until - MAX_TRACK_WINDOW

    clamped = False

    if since < minimum_since:
        since = minimum_since
        clamped = True

    aircraft_id = icao24.lower()

    database_pool = db_pool()

    # Fetch one extra row so we can truthfully say whether the
    # response was truncated without issuing a second COUNT query.
    query_limit = limit + 1

    async with database_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                time,
                time_position,
                latitude,
                longitude,
                baro_altitude,
                geo_altitude,
                velocity,
                true_track,
                vertical_rate,
                on_ground
            FROM state_vectors
            WHERE icao24 = $1
              AND time >= $2
              AND time < $3
            ORDER BY time DESC
            LIMIT $4
            """,
            aircraft_id,
            since,
            until,
            query_limit,
        )

    truncated = len(rows) > limit

    if truncated:
        rows = rows[:limit]

    points = [
        {
            "time": row["time"].isoformat(),
            "time_position": (
                row["time_position"].isoformat()
                if row["time_position"] is not None
                else None
            ),
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "baro_altitude": row["baro_altitude"],
            "geo_altitude": row["geo_altitude"],
            "velocity": row["velocity"],
            "true_track": row["true_track"],
            "vertical_rate": row["vertical_rate"],
            "on_ground": row["on_ground"],
        }
        for row in rows
    ]

    return {
        "icao24": aircraft_id,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "count": len(points),
        "limit": limit,
        "truncated": truncated,
        "window_clamped": clamped,
        "points": points,
    }