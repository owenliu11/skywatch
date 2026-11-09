"""Bounded, strictly ordered observations on the live ingest path."""

from collections import deque
from dataclasses import dataclass
from math import isfinite

from app.models import StateVector

MIN_SAMPLES = 5
MAX_SAMPLES = 10
MAX_SPAN_S = 180.0
# Conservative shared threshold: maximum active-region p99; see docs/gap_distribution.txt.
GAP_S = 36.0
DT_FLOOR_S = 1.0


@dataclass(frozen=True, slots=True)
class Sample:
    last_contact: float
    time_position: float | None
    lat: float
    lon: float
    baro_alt: float | None
    geo_alt: float | None
    velocity: float | None
    true_track: float | None
    vertical_rate: float | None
    on_ground: bool
    region: str


class TrackWindows:
    def __init__(self, gap_s: float = GAP_S) -> None:
        self.gap_s = gap_s
        self._windows: dict[str, deque[Sample]] = {}

    def append(self, sv: StateVector, region: str) -> bool:
        if sv.latitude is None or sv.longitude is None:
            return False
        return self.append_sample(
            sv.icao24,
            Sample(
                float(sv.last_contact),
                float(sv.time_position) if sv.time_position is not None else None,
                sv.latitude,
                sv.longitude,
                sv.baro_altitude,
                sv.geo_altitude,
                sv.velocity,
                sv.true_track,
                sv.vertical_rate,
                sv.on_ground,
                region,
            ),
        )

    def append_sample(self, icao24: str, sample: Sample) -> bool:
        """Shared acceptance policy for live input and offline replay."""
        if not all(isfinite(v) for v in (sample.last_contact, sample.lat, sample.lon)):
            return False
        window = self._windows.get(icao24)
        if window and sample.last_contact <= window[-1].last_contact:
            return False
        if window is None:
            window = self._windows[icao24] = deque(maxlen=MAX_SAMPLES)
        if window and sample.last_contact - window[-1].last_contact > self.gap_s:
            window.clear()
        window.append(sample)
        while sample.last_contact - window[0].last_contact > MAX_SPAN_S:
            window.popleft()
        return True

    def get(self, icao24: str) -> tuple[Sample, ...]:
        return tuple(self._windows.get(icao24, ()))

    def snapshot(self) -> dict[str, tuple[Sample, ...]]:
        return {key: tuple(value) for key, value in self._windows.items()}

    def evict_before(self, cutoff: float) -> int:
        stale = [key for key, value in self._windows.items() if value[-1].last_contact < cutoff]
        for key in stale:
            del self._windows[key]
        return len(stale)

    def is_ready(self, icao24: str) -> bool:
        return len(self._windows.get(icao24, ())) >= MIN_SAMPLES

    @property
    def ready_count(self) -> int:
        return sum(len(value) >= MIN_SAMPLES for value in self._windows.values())

    @property
    def ready(self) -> bool:
        return self.ready_count > 0

    def __len__(self) -> int:
        return len(self._windows)
