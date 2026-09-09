from datetime import UTC, datetime

from app.models import StateVector
from app.state import (
    LOST_AFTER_S,
    LiveState,
)


def make_state(
    *,
    icao24: str = "abc123",
    last_contact: int = 1_700_000_000,
    latitude: float = 37.7,
    longitude: float = -122.4,
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


def test_upsert_accepts_newer_state() -> None:
    live = LiveState()

    first = make_state(
        last_contact=1_700_000_000,
        latitude=37.7,
    )

    newer = make_state(
        last_contact=1_700_000_010,
        latitude=37.8,
    )

    assert live.upsert(first, "bay_area") is True
    assert live.upsert(newer, "bay_area") is True

    record = live.snapshot()["abc123"]

    assert record.state == newer
    assert record.state.latitude == 37.8
    assert len(live) == 1


def test_upsert_rejects_duplicate_and_older_state() -> None:
    live = LiveState()

    newest = make_state(
        last_contact=1_700_000_010,
        latitude=37.8,
    )

    duplicate = make_state(
        last_contact=1_700_000_010,
        latitude=38.8,
    )

    older = make_state(
        last_contact=1_700_000_000,
        latitude=36.8,
    )

    assert live.upsert(newest, "bay_area") is True

    assert live.upsert(duplicate, "bay_area") is False
    assert live.upsert(older, "bay_area") is False

    record = live.snapshot()["abc123"]

    assert record.state == newest
    assert record.state.latitude == 37.8


def test_evict_before_removes_old_aircraft() -> None:
    live = LiveState()

    old_state = make_state(
        icao24="old001",
        last_contact=1_700_000_000,
    )

    fresh_state = make_state(
        icao24="new001",
        last_contact=1_700_000_100,
    )

    live.upsert(old_state, "bay_area")
    live.upsert(fresh_state, "bay_area")

    cutoff = datetime.fromtimestamp(
        1_700_000_050,
        tz=UTC,
    )

    removed = live.evict_before(cutoff)

    current = live.snapshot()

    assert removed == 1
    assert "old001" not in current
    assert "new001" in current
    assert len(live) == 1

def test_lost_threshold_has_tail_latency_margin() -> None:
    assert LOST_AFTER_S == 60.0
    assert LOST_AFTER_S > 29.0