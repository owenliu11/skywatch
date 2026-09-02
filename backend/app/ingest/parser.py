"""Parse OpenSky's positional state-vector arrays.

OpenSky returns state vectors as arrays-of-arrays rather than objects, to save
bandwidth. A row is 17 fields (indices 0-16). Index 17, `category`, exists only
when the request was made with extended=1 -- so rows of both widths are valid
and the parser must handle either without guessing.

Design decision: parse errors are RETURNED, not raised. One malformed record in
a snapshot of 400 must not discard the other 399. This is also the Phase 5
failure drill "feed malformed state vectors" passed in advance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.models import StatesSnapshot, StateVector

# Named indices. Never index a raw row by literal number anywhere else.
IDX_ICAO24 = 0
IDX_CALLSIGN = 1
IDX_ORIGIN_COUNTRY = 2
IDX_TIME_POSITION = 3
IDX_LAST_CONTACT = 4
IDX_LONGITUDE = 5
IDX_LATITUDE = 6
IDX_BARO_ALTITUDE = 7
IDX_ON_GROUND = 8
IDX_VELOCITY = 9
IDX_TRUE_TRACK = 10
IDX_VERTICAL_RATE = 11
IDX_SENSORS = 12  # null unless the request filtered by sensor; unused
IDX_GEO_ALTITUDE = 13
IDX_SQUAWK = 14
IDX_SPI = 15
IDX_POSITION_SOURCE = 16
IDX_CATEGORY = 17  # present only with extended=1

VALID_ROW_LENGTHS = (17, 18)


@dataclass(frozen=True)
class ParseError:
    """One record that failed, kept for logging and metrics."""

    index: int
    icao24: str | None
    reason: str


@dataclass(frozen=True)
class ParseResult:
    snapshot: StatesSnapshot
    errors: tuple[ParseError, ...]

    @property
    def error_count(self) -> int:
        return len(self.errors)


def parse_state_vector(row: list[Any]) -> StateVector:
    """Turn one positional row into a StateVector. Raises on bad input."""
    if len(row) not in VALID_ROW_LENGTHS:
        raise ValueError(f"row has {len(row)} fields, expected >= {VALID_ROW_LENGTHS}")

    # Length check, not try/except IndexError: the absence of category is a
    # normal, documented shape, not an exceptional condition.
    category = row[IDX_CATEGORY] if len(row) > IDX_CATEGORY else None

    return StateVector(
        icao24=row[IDX_ICAO24],
        callsign=row[IDX_CALLSIGN],
        origin_country=row[IDX_ORIGIN_COUNTRY],
        time_position=row[IDX_TIME_POSITION],
        last_contact=row[IDX_LAST_CONTACT],
        longitude=row[IDX_LONGITUDE],
        latitude=row[IDX_LATITUDE],
        baro_altitude=row[IDX_BARO_ALTITUDE],
        on_ground=row[IDX_ON_GROUND],
        velocity=row[IDX_VELOCITY],
        true_track=row[IDX_TRUE_TRACK],
        vertical_rate=row[IDX_VERTICAL_RATE],
        sensors=row[IDX_SENSORS],
        geo_altitude=row[IDX_GEO_ALTITUDE],
        squawk=row[IDX_SQUAWK],
        spi=row[IDX_SPI],
        position_source=row[IDX_POSITION_SOURCE],
        category=category,
    )


def parse_states(payload: dict[str, Any], region: str) -> ParseResult:
    """Parse a full /states/all response body.

    `payload["states"]` is null (not []) when the box is empty -- a real
    response shape at 4am over a sparse region, so handle it explicitly.
    """
    rows = payload.get("states") or []
    states: list[StateVector] = []
    errors: list[ParseError] = []

    for i, row in enumerate(rows):
        try:
            states.append(parse_state_vector(row))
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            icao24 = row[IDX_ICAO24] if isinstance(row, list) and row else None
            errors.append(
                ParseError(
                    index=i,
                    icao24=icao24 if isinstance(icao24, str) else None,
                    # Pydantic errors are multi-line; flatten rather than
                    # truncate at the first newline, or the useful part
                    # (which field, what was wrong) is the part you lose.
                    reason=" ".join(
                        f"{type(exc).__name__}: {exc}".split()
                    )[:200],
                )
            )

    snapshot = StatesSnapshot(
        time=payload["time"],
        region=region,
        states=tuple(states),
    )
    return ParseResult(snapshot=snapshot, errors=tuple(errors))