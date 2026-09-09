"""Live aircraft WebSocket protocol."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from json import JSONDecodeError
from math import ceil, floor
from time import perf_counter

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.state import (
    LOST_AFTER_S,
    LiveRecord,
    LiveState,
)

router = APIRouter()
OUTBOUND_QUEUE_MAX = 4
MAX_CONNECTIONS = 200
TICK_INTERVAL_S = 1.0

@dataclass
class RealtimeMetrics:
    """Process-lifetime counters; resyncs count drop-triggered requests."""

    frames_sent_total: int = 0
    frames_dropped_total: int = 0
    resyncs_total: int = 0
    tick_ms_last: float = 0.0
    tick_samples: deque[float] = field(default_factory=lambda: deque(maxlen=300))

    def record_tick(self, elapsed_ms: float) -> None:
        self.tick_ms_last = elapsed_ms
        self.tick_samples.append(elapsed_ms)

    @property
    def tick_ms_p95(self) -> float:
        if not self.tick_samples:
            return 0.0
        samples = sorted(self.tick_samples)
        return samples[ceil(len(samples) * 0.95) - 1]


metrics = RealtimeMetrics()

Grid = dict[tuple[int, int], list[LiveRecord]]
FIELDS = [
    "icao24",
    "lat",
    "lon",
    "baro_alt",
    "track",
    "velocity",
    "vrate",
    "on_ground",
    "time_position",
    "last_contact",
    "callsign",
]

@dataclass(frozen=True, slots=True)
class Viewport:
    """Geographic viewport sent by a WebSocket client."""

    south: float
    west: float
    north: float
    east: float

    def contains(
        self,
        record: LiveRecord,
    ) -> bool:
        """Return whether a live aircraft lies inside the viewport."""

        state = record.state

        if state.latitude is None or state.longitude is None:
            return False

        return (
            self.south <= state.latitude <= self.north
            and self.west <= state.longitude <= self.east
        )


@dataclass
class Connection:
    """Per-client WebSocket state."""

    ws: WebSocket

    viewport: Viewport | None = None

    out: asyncio.Queue[dict[str, object]] = field(
        default_factory=lambda: asyncio.Queue(
            maxsize=OUTBOUND_QUEUE_MAX
        )
    )

    # ICAO24 -> last_contact last sent to this client.
    known: dict[str, int] = field(default_factory=dict)

    seq: int = 0
    needs_snapshot: bool = True
    dropped_frames: int = 0

class ConnectionManager:
    """Track active WebSocket clients."""

    def __init__(
        self,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self._connections: list[Connection] = []
        self.max_connections = max_connections

    def at_capacity(self) -> bool:
        """Return whether new clients should be rejected."""

        return len(self._connections) >= self.max_connections

    def add(
        self,
        ws: WebSocket,
    ) -> Connection:
        """Register a new connection."""

        connection = Connection(ws=ws)

        self._connections.append(connection)

        return connection

    def remove(
        self,
        connection: Connection,
    ) -> None:
        """Remove a connection if still registered."""

        try:
            self._connections.remove(connection)
        except ValueError:
            pass

    def connections(self) -> tuple[Connection, ...]:
        """Return a stable snapshot of active connections."""

        return tuple(self._connections)

    def __len__(self) -> int:
        return len(self._connections)

manager = ConnectionManager()

def parse_viewport_message(
    message: object,
) -> Viewport:
    """Validate a client viewport message."""

    if not isinstance(message, dict):
        raise TypeError("message must be an object")

    if message.get("t") != "viewport":
        raise ValueError("unsupported message type")

    bbox = message.get("bbox")

    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError(
            "viewport bbox must contain four values"
        )

    if not all(
        isinstance(value, int | float)
        for value in bbox
    ):
        raise ValueError(
            "viewport bbox values must be numbers"
        )

    south, west, north, east = (
        float(value) for value in bbox
    )

    if not -90 <= south <= 90:
        raise ValueError("south latitude out of range")

    if not -90 <= north <= 90:
        raise ValueError("north latitude out of range")

    if not -180 <= west <= 180:
        raise ValueError("west longitude out of range")

    if not -180 <= east <= 180:
        raise ValueError("east longitude out of range")

    if south > north:
        raise ValueError(
            "south must not exceed north"
        )

    if west > east:
        raise ValueError(
            "antimeridian viewport is not supported"
        )

    return Viewport(
        south=south,
        west=west,
        north=north,
        east=east,
    )

def handle_client_message(
    connection: Connection,
    message: object,
) -> None:
    """Apply one validated client message."""

    viewport = parse_viewport_message(message)

    connection.viewport = viewport

    # A viewport change always resets diff state.
    connection.known.clear()
    connection.needs_snapshot = True

def record_to_row(
    record: LiveRecord,
) -> list[object]:
    """Convert one live aircraft into the WebSocket wire format."""

    state = record.state

    return [
        state.icao24,
        state.latitude,
        state.longitude,
        state.baro_altitude,
        state.true_track,
        state.velocity,
        state.vertical_rate,
        state.on_ground,
        state.time_position,
        state.last_contact,
        state.callsign,
    ]

def build_frame(
    connection: Connection,
    visible: list[LiveRecord],
    now: datetime,
) -> dict[str, object] | None:
    """Build a snapshot or delta frame for one connection."""

    current = {
        record.state.icao24: record
        for record in visible
    }

    connection.seq += 1

    if connection.needs_snapshot:
        rows = [
            record_to_row(record)
            for record in current.values()
        ]

        connection.known = {
            icao24: record.state.last_contact
            for icao24, record in current.items()
            if record.state.last_contact is not None
        }

        connection.needs_snapshot = False

        return {
            "t": "snapshot",
            "seq": connection.seq,
            "server_time": now.timestamp(),
            "fields": FIELDS,
            "aircraft": rows,
        }

    enter: list[list[object]] = []
    update: list[list[object]] = []

    previous_ids = set(connection.known)
    current_ids = set(current)

    leave = sorted(previous_ids - current_ids)

    for icao24, record in current.items():
        last_contact = record.state.last_contact

        if last_contact is None:
            continue

        previous = connection.known.get(icao24)

        if previous is None:
            enter.append(record_to_row(record))
        elif last_contact > previous:
            update.append(record_to_row(record))

    if not enter and not update and not leave:
        connection.seq -= 1
        return None

    connection.known = {
        icao24: record.state.last_contact
        for icao24, record in current.items()
        if record.state.last_contact is not None
    }

    return {
        "t": "delta",
        "seq": connection.seq,
        "server_time": now.timestamp(),
        "enter": enter,
        "update": update,
        "leave": leave,
    }

def offer_frame(
    connection: Connection,
    frame: dict[str, object],
) -> bool:
    """Offer a frame without ever blocking the producer."""

    try:
        connection.out.put_nowait(frame)
        return True
    except asyncio.QueueFull:
        connection.dropped_frames += 1
        metrics.frames_dropped_total += 1
        metrics.resyncs_total += 1
        connection.needs_snapshot = True
        connection.known.clear()
        return False
    
async def sender_loop(
    connection: Connection,
) -> None:
    """Send queued frames to one WebSocket client."""

    while True:
        frame = await connection.out.get()

        try:
            await connection.ws.send_json(frame)
            metrics.frames_sent_total += 1
        finally:
            connection.out.task_done()

async def receiver_loop(
    connection: Connection,
) -> None:
    """Receive viewport updates from one browser."""

    while True:
        try:
            message = await connection.ws.receive_json()

            handle_client_message(
                connection,
                message,
            )

        except JSONDecodeError:
            await connection.ws.send_json(
                {
                    "t": "error",
                    "detail": "message must contain valid JSON",
                }
            )

        except (TypeError, ValueError) as exc:
            await connection.ws.send_json(
                {
                    "t": "error",
                    "detail": str(exc),
                }
            )

def build_grid(
    states: dict[str, LiveRecord],
) -> Grid:
    """Bucket positioned aircraft into 1-degree geographic cells."""

    grid: Grid = {}

    for record in states.values():
        state = record.state

        if state.latitude is None or state.longitude is None:
            continue

        cell = (
            floor(state.latitude),
            floor(state.longitude),
        )

        grid.setdefault(cell, []).append(record)

    return grid

def candidates(
    grid: Grid,
    viewport: Viewport,
) -> list[LiveRecord]:
    """Return aircraft inside the grid cells touched by a viewport."""

    visible: list[LiveRecord] = []

    south_cell = floor(viewport.south)
    north_cell = floor(viewport.north)
    west_cell = floor(viewport.west)
    east_cell = floor(viewport.east)

    for lat_cell in range(
        south_cell,
        north_cell + 1,
    ):
        for lon_cell in range(
            west_cell,
            east_cell + 1,
        ):
            records = grid.get(
                (lat_cell, lon_cell),
                [],
            )

            for record in records:
                if viewport.contains(record):
                    visible.append(record)

    return visible


async def broadcast_loop(
    live: LiveState,
    connection_manager: ConnectionManager,
) -> None:
    """Broadcast viewport-filtered live updates to all clients."""

    while True:
        tick_started = perf_counter()
        now = datetime.now(UTC)
        cutoff = now - timedelta(
            seconds=LOST_AFTER_S,
        )
        live.evict_before(cutoff)
        states = live.snapshot()
        grid = build_grid(states)

        for connection in connection_manager.connections():
            viewport = connection.viewport

            if viewport is None:
                continue

            visible = candidates(
                grid,
                viewport,
            )

            frame = build_frame(
                connection,
                visible,
                now,
            )

            if frame is None:
                continue

            offer_frame(
                connection,
                frame,
            )

        metrics.record_tick((perf_counter() - tick_started) * 1000)
        await asyncio.sleep(TICK_INTERVAL_S)



@router.websocket("/ws/live")
async def live_websocket(
    websocket: WebSocket,
) -> None:
    """Serve live aircraft snapshots and deltas."""

    origin = websocket.headers.get("origin")
    allowed = {value.strip() for value in settings.ws_allowed_origins.split(",")}
    if origin is not None and origin not in allowed:
        await websocket.close(code=1008, reason="origin not allowed")
        return

    if manager.at_capacity():
        await websocket.close(
            code=1013,
            reason="server at WebSocket capacity",
        )
        return

    await websocket.accept()

    connection = manager.add(websocket)

  
    sender_task = asyncio.create_task(
        sender_loop(connection)
    )

    receiver_task = asyncio.create_task(
        receiver_loop(connection)
    )



    try:
        await receiver_task

    except WebSocketDisconnect:
        pass

    finally:
        manager.remove(connection)

        sender_task.cancel()
        receiver_task.cancel()

        await asyncio.gather(
            sender_task,
            receiver_task,
            return_exceptions=True,
        )