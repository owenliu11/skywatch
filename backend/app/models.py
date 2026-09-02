"""Domain models for OpenSky state vectors.

Units are SI exactly as the API delivers them: metres, metres/second.
Conversion to knots and feet-per-minute happens once, in detect/features.py.
Mixing units at the storage layer is how you get a detector that fires on
unit-conversion bugs instead of aircraft.

Nullability is load-bearing. Every field the OpenSky docs mark "can be null"
is `| None` here with no default. Coercing a missing altitude to 0.0 would
fabricate a 30,000-foot descent in the Phase 4 vertical-rate feature.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


class StateVector(BaseModel):
    """One aircraft at one instant."""

    model_config = ConfigDict(frozen=True)

    # Always present.
    icao24: str
    origin_country: str
    last_contact: int
    on_ground: bool
    spi: bool
    position_source: int

    # Documented as nullable. No defaults on purpose: a missing value must
    # be passed explicitly as None by the parser, never silently filled.
    callsign: str | None
    time_position: int | None
    longitude: float | None
    latitude: float | None
    baro_altitude: float | None
    velocity: float | None
    true_track: float | None
    vertical_rate: float | None
    geo_altitude: float | None
    squawk: str | None
    sensors: list[int] | None

    # Index 17. Only returned when the request sets extended=1.
    category: int | None = None

    @field_validator("callsign", "squawk", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        """Callsign is space-padded to 8 chars; empty means 'not received'."""
        if isinstance(v, str):
            stripped = v.strip()
            return stripped or None
        return v

    @field_validator("icao24", mode="before")
    @classmethod
    def _normalise_icao24(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("latitude")
    @classmethod
    def _check_lat(cls, v: float | None) -> float | None:
        if v is not None and not (-90.0 <= v <= 90.0):
            raise ValueError(f"latitude out of range: {v}")
        return v

    @field_validator("longitude")
    @classmethod
    def _check_lon(cls, v: float | None) -> float | None:
        if v is not None and not (-180.0 <= v <= 180.0):
            raise ValueError(f"longitude out of range: {v}")
        return v

    @property
    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class StatesSnapshot(BaseModel):
    """A parsed /states/all response."""

    model_config = ConfigDict(frozen=True)

    time: int
    region: str
    states: tuple[StateVector, ...]

    @property
    def aircraft_count(self) -> int:
        return len(self.states)