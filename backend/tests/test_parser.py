import pytest

from app.ingest.parser import parse_state_vector, parse_states


@pytest.mark.parametrize("index", [3, 4])
def test_out_of_range_row_timestamps_are_rejected_without_losing_valid_rows(index):
    valid = make_row()
    invalid = make_row()
    invalid[index] = 10**14
    result = parse_states({"time": 1_700_000_000, "states": [invalid, valid]}, "test")
    assert result.error_count == 1
    assert len(result.snapshot.states) == 1


def test_out_of_range_snapshot_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timestamp outside supported datetime range"):
        parse_states({"time": 10**14, "states": []}, "test")


def make_row() -> list[object]:
    return [
        "abc123",
        "UAL123  ",
        "United States",
        1_700_000_000,
        1_700_000_001,
        -122.3,
        37.6,
        10_000.0,
        False,
        250.0,
        180.0,
        -2.5,
        None,
        10_200.0,
        "1200",
        False,
        0,
    ]


def test_parse_17_field_state_vector() -> None:
    state = parse_state_vector(make_row())

    assert state.icao24 == "abc123"
    assert state.callsign == "UAL123"
    assert state.longitude == -122.3
    assert state.latitude == 37.6
    assert state.category is None


def test_parse_18_field_extended_state_vector() -> None:
    row = make_row()
    row.append(3)

    state = parse_state_vector(row)

    assert state.category == 3


def test_null_values_remain_none() -> None:
    row = make_row()
    row[7] = None
    row[9] = None
    row[11] = None

    state = parse_state_vector(row)

    assert state.baro_altitude is None
    assert state.velocity is None
    assert state.vertical_rate is None


def test_states_none_produces_empty_snapshot() -> None:
    result = parse_states(
        {
            "time": 1_700_000_000,
            "states": None,
        },
        region="bay_area",
    )

    assert result.snapshot.aircraft_count == 0
    assert result.error_count == 0


def test_bad_record_does_not_discard_good_record() -> None:
    good_row = make_row()
    bad_row = ["too", "short"]

    result = parse_states(
        {
            "time": 1_700_000_000,
            "states": [good_row, bad_row],
        },
        region="bay_area",
    )

    assert result.snapshot.aircraft_count == 1
    assert result.error_count == 1
    assert result.errors[0].index == 1


def test_invalid_latitude_becomes_parse_error() -> None:
    row = make_row()
    row[6] = 137.0

    result = parse_states(
        {
            "time": 1_700_000_000,
            "states": [row],
        },
        region="bay_area",
    )

    assert result.snapshot.aircraft_count == 0
    assert result.error_count == 1
    assert "latitude" in result.errors[0].reason


def test_invalid_row_width_raises() -> None:
    with pytest.raises(ValueError):
        parse_state_vector(["too", "short"])

def test_extra_fields_are_rejected() -> None:
    row = make_row()
    row.extend([3, "unexpected"])

    with pytest.raises(ValueError):
        parse_state_vector(row)
