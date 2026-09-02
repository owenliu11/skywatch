import pytest

from app.config import MAX_POLL_INTERVAL_S, MIN_POLL_INTERVAL_S, REGIONS
from app.ingest.scheduler import next_interval


@pytest.fixture
def bay_area():
    return REGIONS["bay_area"]


def test_normal_density_stays_near_base(bay_area) -> None:
    result = next_interval(
        current_s=15.0,
        aircraft_seen=60,
        budget_fraction=1.0,
        region=bay_area,
    )

    assert result == 15.0


def test_dense_airspace_tightens_interval(bay_area) -> None:
    result = next_interval(
        current_s=15.0,
        aircraft_seen=150,
        budget_fraction=1.0,
        region=bay_area,
    )

    assert result < 15.0


def test_sparse_airspace_widens_interval(bay_area) -> None:
    result = next_interval(
        current_s=15.0,
        aircraft_seen=5,
        budget_fraction=1.0,
        region=bay_area,
    )

    assert result > 15.0


def test_low_budget_widens_interval(bay_area) -> None:
    result = next_interval(
        current_s=15.0,
        aircraft_seen=60,
        budget_fraction=0.10,
        region=bay_area,
    )

    assert result > 15.0


def test_budget_at_threshold_does_not_widen(bay_area) -> None:
    result = next_interval(
        current_s=15.0,
        aircraft_seen=60,
        budget_fraction=0.20,
        region=bay_area,
    )

    assert result == 15.0


def test_zero_budget_is_safe(bay_area) -> None:
    result = next_interval(
        current_s=15.0,
        aircraft_seen=60,
        budget_fraction=0.0,
        region=bay_area,
    )

    assert result > 15.0
    assert result <= MAX_POLL_INTERVAL_S


def test_negative_budget_fraction_is_clamped(bay_area) -> None:
    negative = next_interval(
        current_s=15.0,
        aircraft_seen=60,
        budget_fraction=-1.0,
        region=bay_area,
    )

    zero = next_interval(
        current_s=15.0,
        aircraft_seen=60,
        budget_fraction=0.0,
        region=bay_area,
    )

    assert negative == zero


def test_budget_fraction_above_one_is_clamped(bay_area) -> None:
    above_one = next_interval(
        current_s=15.0,
        aircraft_seen=60,
        budget_fraction=2.0,
        region=bay_area,
    )

    one = next_interval(
        current_s=15.0,
        aircraft_seen=60,
        budget_fraction=1.0,
        region=bay_area,
    )

    assert above_one == one


def test_interval_never_goes_below_minimum(bay_area) -> None:
    result = next_interval(
        current_s=5.0,
        aircraft_seen=500,
        budget_fraction=1.0,
        region=bay_area,
    )

    assert result >= MIN_POLL_INTERVAL_S


def test_interval_never_exceeds_maximum(bay_area) -> None:
    result = next_interval(
        current_s=300.0,
        aircraft_seen=0,
        budget_fraction=0.0,
        region=bay_area,
    )

    assert result <= MAX_POLL_INTERVAL_S


def test_smoothing_moves_halfway_toward_sparse_target(bay_area) -> None:
    # Bay Area base = 15s.
    # Sparse target = 30s.
    # Smoothing = 0.5.
    #
    # 15 + 0.5 * (30 - 15) = 22.5
    result = next_interval(
        current_s=15.0,
        aircraft_seen=0,
        budget_fraction=1.0,
        region=bay_area,
    )

    assert result == 22.5


def test_smoothing_moves_halfway_toward_dense_target(bay_area) -> None:
    # Bay Area base = 15s.
    # Dense target = 7.5s.
    #
    # 15 + 0.5 * (7.5 - 15) = 11.25
    result = next_interval(
        current_s=15.0,
        aircraft_seen=200,
        budget_fraction=1.0,
        region=bay_area,
    )

    assert result == 11.25