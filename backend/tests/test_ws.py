from datetime import UTC, datetime

import pytest

from app.api.ws import (
    Connection,
    Viewport,
    build_frame,
    build_grid,
    candidates,
    handle_client_message,
    offer_frame,
    parse_viewport_message,
)
from app.models import StateVector
from app.state import LiveRecord


class DummyWebSocket:
    pass

def make_record(
    *,
    icao24: str = "abc123",
    last_contact: int = 1_700_000_000,
    latitude: float | None = 37.7,
    longitude: float | None = -122.4,
) -> LiveRecord:
    state = StateVector(
        icao24=icao24,
        origin_country="United States",
        last_contact=last_contact,
        on_ground=False,
        spi=False,
        position_source=0,
        callsign="TEST123",
        time_position=last_contact,
        longitude=longitude,
        latitude=latitude,
        baro_altitude=10_000.0,
        velocity=200.0,
        true_track=90.0,
        vertical_rate=0.0,
        geo_altitude=10_050.0,
        squawk=None,
        sensors=None,
        category=3,
    )

    return LiveRecord(
        state=state,
        region="bay_area",
        received_at=datetime.now(UTC),
    )
def test_viewport_contains_inside_aircraft() -> None:
    viewport = Viewport(
        south=37.2,
        west=-122.6,
        north=38.1,
        east=-121.7,
    )

    assert viewport.contains(make_record()) is True


def test_viewport_excludes_outside_aircraft() -> None:
    viewport = Viewport(
        south=37.2,
        west=-122.6,
        north=38.1,
        east=-121.7,
    )

    record = make_record(
        latitude=40.0,
        longitude=-120.0,
    )

    assert viewport.contains(record) is False


def test_viewport_includes_edges() -> None:
    viewport = Viewport(
        south=37.2,
        west=-122.6,
        north=38.1,
        east=-121.7,
    )

    record = make_record(
        latitude=37.2,
        longitude=-122.6,
    )

    assert viewport.contains(record) is True


def test_viewport_excludes_null_position() -> None:
    viewport = Viewport(
        south=37.2,
        west=-122.6,
        north=38.1,
        east=-121.7,
    )

    record = make_record(
        latitude=None,
        longitude=None,
    )

    assert viewport.contains(record) is False


def test_parse_viewport_rejects_antimeridian() -> None:
    with pytest.raises(ValueError):
        parse_viewport_message(
            {
                "t": "viewport",
                "bbox": [30, 170, 40, -170],
            }
        )


def test_viewport_change_forces_snapshot() -> None:
    connection = Connection(
        ws=DummyWebSocket(),  # type: ignore[arg-type]
    )

    connection.known["abc123"] = 1_700_000_000
    connection.needs_snapshot = False

    handle_client_message(
        connection,
        {
            "t": "viewport",
            "bbox": [
                37.2,
                -122.6,
                38.1,
                -121.7,
            ],
        },
    )

    assert connection.viewport is not None
    assert connection.known == {}
    assert connection.needs_snapshot is True


def test_first_frame_is_snapshot() -> None:
    connection = Connection(
        ws=DummyWebSocket(),  # type: ignore[arg-type]
    )

    now = datetime.now(UTC)

    frame = build_frame(
        connection,
        [make_record()],
        now,
    )

    assert frame is not None
    assert frame["t"] == "snapshot"
    assert frame["seq"] == 1
    assert len(frame["aircraft"]) == 1
    assert connection.needs_snapshot is False
    assert "abc123" in connection.known

def test_delta_enter_update_leave() -> None:
    connection = Connection(
        ws=DummyWebSocket(),  # type: ignore[arg-type]
    )

    now = datetime.now(UTC)

    first = make_record(
        icao24="abc123",
        last_contact=1_700_000_000,
    )

    snapshot = build_frame(
        connection,
        [first],
        now,
    )

    assert snapshot is not None

    updated = make_record(
        icao24="abc123",
        last_contact=1_700_000_010,
    )

    entered = make_record(
        icao24="new456",
        last_contact=1_700_000_005,
    )

    delta = build_frame(
        connection,
        [updated, entered],
        now,
    )

    assert delta is not None
    assert delta["t"] == "delta"

    update_rows = delta["update"]
    enter_rows = delta["enter"]

    assert update_rows[0][0] == "abc123"
    assert enter_rows[0][0] == "new456"

    leave_delta = build_frame(
        connection,
        [entered],
        now,
    )

    assert leave_delta is not None
    assert leave_delta["leave"] == ["abc123"]

def test_unchanged_aircraft_produces_no_delta() -> None:
    connection = Connection(
        ws=DummyWebSocket(),  # type: ignore[arg-type]
    )

    record = make_record()

    now = datetime.now(UTC)

    snapshot = build_frame(
        connection,
        [record],
        now,
    )

    assert snapshot is not None

    frame = build_frame(
        connection,
        [record],
        now,
    )

    assert frame is None
    assert connection.seq == 1

def test_offer_frame_enqueues_when_space_exists() -> None:
    connection = Connection(
        ws=DummyWebSocket(),  # type: ignore[arg-type]
    )

    frame = {
        "t": "snapshot",
        "seq": 1,
    }

    accepted = offer_frame(
        connection,
        frame,
    )

    assert accepted is True
    assert connection.out.qsize() == 1
    assert connection.dropped_frames == 0

def test_full_queue_forces_snapshot_resync() -> None:
    connection = Connection(
        ws=DummyWebSocket(),  # type: ignore[arg-type]
    )

    connection.needs_snapshot = False
    connection.known["abc123"] = 1_700_000_000

    for index in range(connection.out.maxsize):
        connection.out.put_nowait(
            {
                "t": "delta",
                "seq": index + 1,
            }
        )

    accepted = offer_frame(
        connection,
        {
            "t": "delta",
            "seq": 999,
        },
    )

    assert accepted is False
    assert connection.dropped_frames == 1
    assert connection.needs_snapshot is True
    assert connection.known == {}

def test_dropped_frame_causes_next_frame_to_be_snapshot() -> None:
    connection = Connection(
        ws=DummyWebSocket(),  # type: ignore[arg-type]
    )

    now = datetime.now(UTC)

    record = make_record()

    first = build_frame(
        connection,
        [record],
        now,
    )

    assert first is not None
    assert first["t"] == "snapshot"

    for index in range(connection.out.maxsize):
        connection.out.put_nowait(
            {
                "t": "delta",
                "seq": index + 1,
            }
        )

    accepted = offer_frame(
        connection,
        {
            "t": "delta",
            "seq": 999,
        },
    )

    assert accepted is False
    assert connection.needs_snapshot is True

    next_frame = build_frame(
        connection,
        [record],
        now,
    )

    assert next_frame is not None
    assert next_frame["t"] == "snapshot"

def test_grid_candidates_return_aircraft_inside_viewport() -> None:
    inside = make_record(
        icao24="inside1",
        latitude=37.7,
        longitude=-122.4,
    )

    outside = make_record(
        icao24="outside1",
        latitude=40.0,
        longitude=-120.0,
    )

    grid = build_grid(
        {
            "inside1": inside,
            "outside1": outside,
        }
    )

    viewport = Viewport(
        south=37.2,
        west=-122.6,
        north=38.1,
        east=-121.7,
    )

    visible = candidates(
        grid,
        viewport,
    )

    ids = {
        record.state.icao24
        for record in visible
    }

    assert ids == {"inside1"}

def test_grid_skips_aircraft_without_position() -> None:
    positioned = make_record(
        icao24="positioned",
    )

    missing = make_record(
        icao24="missing",
        latitude=None,
        longitude=None,
    )

    grid = build_grid(
        {
            "positioned": positioned,
            "missing": missing,
        }
    )

    all_ids = {
        record.state.icao24
        for records in grid.values()
        for record in records
    }

    assert "positioned" in all_ids
    assert "missing" not in all_ids