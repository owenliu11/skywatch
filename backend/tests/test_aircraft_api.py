
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient

import app.api.tracks as tracks_module
import app.main as main_module
from app.models import StateVector
from app.state import LiveState


class TestPool:
    """Expose one test connection through the pool.acquire() interface."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

def make_state(
    *,
    icao24: str,
    latitude: float | None,
    longitude: float | None,
    last_contact: int = 1_700_000_000,
) -> StateVector:
    return StateVector(
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


def test_aircraft_bbox_returns_only_visible_aircraft() -> None:
    live = LiveState()

    live.upsert(
        make_state(
            icao24="inside1",
            latitude=37.7,
            longitude=-122.4,
        ),
        "bay_area",
    )

    live.upsert(
        make_state(
            icao24="outside1",
            latitude=40.0,
            longitude=-120.0,
        ),
        "bay_area",
    )

    main_module.app.state.live = live

    client = TestClient(main_module.app)

    response = client.get(
        "/aircraft",
        params={
            "bbox": "37.2,-122.6,38.1,-121.7",
        },
    )

    assert response.status_code == 200

    body = response.json()

    assert body["count"] == 1
    assert body["truncated"] is False
    assert body["aircraft"][0]["icao24"] == "inside1"


def test_aircraft_bbox_includes_edges() -> None:
    live = LiveState()

    live.upsert(
        make_state(
            icao24="edge1",
            latitude=37.2,
            longitude=-122.6,
        ),
        "bay_area",
    )

    main_module.app.state.live = live

    client = TestClient(main_module.app)

    response = client.get(
        "/aircraft",
        params={
            "bbox": "37.2,-122.6,38.1,-121.7",
        },
    )

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_aircraft_bbox_skips_null_position() -> None:
    live = LiveState()

    live.upsert(
        make_state(
            icao24="nopos1",
            latitude=None,
            longitude=None,
        ),
        "bay_area",
    )

    main_module.app.state.live = live

    client = TestClient(main_module.app)

    response = client.get(
        "/aircraft",
        params={
            "bbox": "37.2,-122.6,38.1,-121.7",
        },
    )

    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_aircraft_bbox_rejects_too_large_area() -> None:
    main_module.app.state.live = LiveState()

    client = TestClient(main_module.app)

    response = client.get(
        "/aircraft",
        params={
            "bbox": "-90,-180,90,180",
        },
    )

    assert response.status_code == 422


def test_aircraft_bbox_rejects_malformed_value() -> None:
    main_module.app.state.live = LiveState()

    client = TestClient(main_module.app)

    response = client.get(
        "/aircraft",
        params={
            "bbox": "37,-122,38",
        },
    )

    assert response.status_code == 422


def test_aircraft_bbox_rejects_antimeridian_crossing() -> None:
    main_module.app.state.live = LiveState()

    client = TestClient(main_module.app)

    response = client.get(
        "/aircraft",
        params={
            "bbox": "30,170,40,-170",
        },
    )

    assert response.status_code == 422

def test_aircraft_by_icao24_returns_current_aircraft() -> None:
    live = LiveState()

    state = make_state(
        icao24="abc123",
        latitude=37.7,
        longitude=-122.4,
    )

    live.upsert(
        state,
        "bay_area",
    )

    main_module.app.state.live = live

    client = TestClient(main_module.app)

    response = client.get(
        "/aircraft/abc123",
    )

    assert response.status_code == 200

    body = response.json()

    assert body["aircraft"]["icao24"] == "abc123"
    assert body["aircraft"]["latitude"] == 37.7
    assert body["aircraft"]["longitude"] == -122.4
    assert body["aircraft"]["region"] == "bay_area"
    assert "as_of" in body


def test_aircraft_by_icao24_is_case_insensitive() -> None:
    live = LiveState()

    live.upsert(
        make_state(
            icao24="abc123",
            latitude=37.7,
            longitude=-122.4,
        ),
        "bay_area",
    )

    main_module.app.state.live = live

    client = TestClient(main_module.app)

    response = client.get(
        "/aircraft/ABC123",
    )

    assert response.status_code == 200
    assert response.json()["aircraft"]["icao24"] == "abc123"


def test_aircraft_by_icao24_returns_404_when_missing() -> None:
    main_module.app.state.live = LiveState()

    client = TestClient(main_module.app)

    response = client.get(
        "/aircraft/ffffff",
    )

    assert response.status_code == 404

    assert response.json() == {
        "detail": "Aircraft not currently available: ffffff"
    }

def test_track_rejects_invalid_time_range() -> None:
    client = TestClient(main_module.app)

    response = client.get(
        "/aircraft/abc123/track",
        params={
            "since": "2026-09-07T23:00:00Z",
            "until": "2026-09-07T22:00:00Z",
        },
    )

    assert response.status_code == 422

    assert response.json() == {
        "detail": "since must be earlier than until"
    }

@pytest.mark.asyncio
async def test_track_clamps_window_to_24_hours(
    db_connection: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_pool = TestPool(db_connection)

    monkeypatch.setattr(
        tracks_module,
        "db_pool",
        lambda: test_pool,
    )

    until = datetime(
        2026,
        9,
        7,
        12,
        0,
        tzinfo=UTC,
    )

    requested_since = until - timedelta(days=7)

    result = await tracks_module.get_aircraft_track(
        icao24="abc123",
        since=requested_since,
        until=until,
        limit=5_000,
    )

    expected_since = until - timedelta(hours=24)

    assert result["window_clamped"] is True
    assert result["since"] == expected_since.isoformat()
    assert result["until"] == until.isoformat()

@pytest.mark.asyncio
async def test_track_sets_truncated_when_limit_bites(
    db_connection: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_pool = TestPool(db_connection)

    monkeypatch.setattr(
        tracks_module,
        "db_pool",
        lambda: test_pool,
    )

    base_time = datetime(
        2026,
        9,
        7,
        12,
        0,
        tzinfo=UTC,
    )

    for seconds in (10, 20, 30):
        observed_at = base_time + timedelta(seconds=seconds)

        await db_connection.execute(
            """
            INSERT INTO state_vectors (
                time,
                icao24,
                time_position,
                latitude,
                longitude,
                baro_altitude,
                geo_altitude,
                velocity,
                true_track,
                vertical_rate,
                on_ground,
                squawk,
                spi,
                position_source,
                region
            )
            VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15
            )
            """,
            observed_at,
            "abc123",
            observed_at,
            37.7,
            -122.4,
            10_000.0,
            10_050.0,
            200.0,
            90.0,
            0.0,
            False,
            None,
            False,
            0,
            "test",
        )

    result = await tracks_module.get_aircraft_track(
        icao24="abc123",
        since=base_time,
        until=base_time + timedelta(minutes=1),
        limit=2,
    )

    assert result["count"] == 2
    assert result["limit"] == 2
    assert result["truncated"] is True
    assert len(result["points"]) == 2