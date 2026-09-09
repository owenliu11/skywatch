"""Live aircraft REST endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request

from app.state import LiveRecord, LiveState

router = APIRouter()

MAX_BBOX_AREA_SQ_DEG = 400.0


@dataclass(frozen=True, slots=True)
class BBox:
    """Validated latitude/longitude bounding box."""

    south: float
    west: float
    north: float
    east: float

    def contains(
        self,
        latitude: float,
        longitude: float,
    ) -> bool:
        """Return whether a coordinate lies inside the box."""

        return (
            self.south <= latitude <= self.north
            and self.west <= longitude <= self.east
        )

    @property
    def area_sq_deg(self) -> float:
        """Return simple bounding-box area in square degrees."""

        return (
            (self.north - self.south)
            * (self.east - self.west)
        )


def parse_bbox(value: str) -> BBox:
    """Parse and validate south,west,north,east."""

    parts = value.split(",")

    if len(parts) != 4:
        raise HTTPException(
            status_code=422,
            detail=(
                "bbox must contain four values: "
                "south,west,north,east"
            ),
        )

    try:
        south, west, north, east = (
            float(part) for part in parts
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="bbox values must be numbers",
        ) from exc

    if not -90 <= south <= 90:
        raise HTTPException(
            status_code=422,
            detail="bbox south latitude is out of range",
        )

    if not -90 <= north <= 90:
        raise HTTPException(
            status_code=422,
            detail="bbox north latitude is out of range",
        )

    if not -180 <= west <= 180:
        raise HTTPException(
            status_code=422,
            detail="bbox west longitude is out of range",
        )

    if not -180 <= east <= 180:
        raise HTTPException(
            status_code=422,
            detail="bbox east longitude is out of range",
        )

    if south > north:
        raise HTTPException(
            status_code=422,
            detail="bbox south must not exceed north",
        )

    # Phase 3 initially rejects antimeridian-crossing boxes explicitly.
    # Silent empty results would be much worse.
    if west > east:
        raise HTTPException(
            status_code=422,
            detail="antimeridian-crossing bbox is not supported",
        )

    bbox = BBox(
        south=south,
        west=west,
        north=north,
        east=east,
    )

    if bbox.area_sq_deg > MAX_BBOX_AREA_SQ_DEG:
        raise HTTPException(
            status_code=422,
            detail=(
                "bbox area exceeds maximum "
                f"{MAX_BBOX_AREA_SQ_DEG} square degrees"
            ),
        )

    return bbox


def serialize_live_record(
    record: LiveRecord,
) -> dict[str, object]:
    """Convert one live record into the public REST shape."""

    state = record.state

    return {
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
        "spi": state.spi,
        "position_source": state.position_source,
        "category": state.category,
        "time_position": state.time_position,
        "last_contact": state.last_contact,
        "region": record.region,
    }


@router.get("/aircraft")
async def get_aircraft(
    request: Request,
    bbox: str = Query(
        ...,
        description="south,west,north,east",
    ),
) -> dict[str, object]:
    """Return current aircraft inside a viewport from LiveState."""

    viewport = parse_bbox(bbox)

    live: LiveState = request.app.state.live

    visible: list[dict[str, object]] = []

    for record in live.snapshot().values():
        state = record.state

        # A missing position is legitimate OpenSky data.
        # It cannot be placed inside a geographic viewport,
        # so preserve the null in LiveState but skip it here.
        if state.latitude is None or state.longitude is None:
            continue

        if not viewport.contains(
            state.latitude,
            state.longitude,
        ):
            continue

        visible.append(
            serialize_live_record(record)
        )

    # Stable ordering makes API behavior and tests deterministic.
    visible.sort(
        key=lambda aircraft: str(aircraft["icao24"])
    )

    return {
        "as_of": datetime.now(UTC).isoformat(),
        "count": len(visible),
        "truncated": False,
        "aircraft": visible,
    }

@router.get("/aircraft/{icao24}")
async def get_aircraft_by_icao24(
    request: Request,
    icao24: str,
) -> dict[str, object]:
    """Return the current state of one aircraft from LiveState."""

    live: LiveState = request.app.state.live

    # OpenSky ICAO24 identifiers are represented as lowercase hex strings.
    aircraft_id = icao24.lower()

    record = live.get(aircraft_id)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Aircraft not currently available: {aircraft_id}",
        )

    return {
        "as_of": datetime.now(UTC).isoformat(),
        "aircraft": serialize_live_record(record),
    }

