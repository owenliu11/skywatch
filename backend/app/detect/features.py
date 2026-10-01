"""SI features. Median motion, peak discontinuities; missing data stays missing."""

from collections import Counter
from dataclasses import asdict, dataclass
from itertools import pairwise
from math import asin, cos, isfinite, radians, sin, sqrt
from statistics import median, pvariance

from app.detect.gaps import RecentGaps
from app.detect.windows import DT_FLOOR_S, GAP_S, MIN_SAMPLES, Sample

SPEED_FLOOR_MS = 25.0
MS_TO_KT = 1.943844
MS_TO_FPM = 196.8504
EARTH_RADIUS_M = 6_371_000.0
FEATURE_COLUMNS = (
    "implied_speed_ms",
    "reported_speed_ms",
    "speed_mismatch_ratio",
    "turn_rate_deg_s",
    "turn_accel_deg_s2",
    "reported_vertical_rate_ms",
    "implied_vertical_rate_ms",
    "vertical_mismatch_ms",
    "jump_distance_m",
    "jump_speed_ms",
    "update_interval_s",
    "interval_variance_s2",
)
METADATA_COLUMNS = {
    "icao24",
    "window_start",
    "window_end",
    "window_span_s",
    "n_samples",
    "n_pairs",
    "spans_gap",
    "on_ground_any",
    "region",
}
assert not set(FEATURE_COLUMNS) & METADATA_COLUMNS


def ang_delta(a: float, b: float) -> float:
    """Signed shortest angle in [-180, 180)."""
    return (a - b + 180.0) % 360.0 - 180.0


def haversine(lat0: float, lon0: float, lat1: float, lon1: float) -> float:
    a, b = radians(lat0), radians(lat1)
    h = sin((b - a) / 2) ** 2 + cos(a) * cos(b) * sin(radians(lon1 - lon0) / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(min(1.0, max(0.0, h))))


@dataclass(frozen=True)
class TrackFeatures:
    implied_speed_ms: float
    reported_speed_ms: float
    speed_mismatch_ratio: float | None
    turn_rate_deg_s: float
    turn_accel_deg_s2: float | None
    reported_vertical_rate_ms: float | None
    implied_vertical_rate_ms: float | None
    vertical_mismatch_ms: float | None
    jump_distance_m: float
    jump_speed_ms: float
    update_interval_s: float
    interval_variance_s2: float
    icao24: str
    window_start: float
    window_end: float
    window_span_s: float
    n_samples: int
    n_pairs: int
    spans_gap: bool
    on_ground_any: bool
    region: str

    def scored(self) -> dict[str, float | None]:
        values = asdict(self)
        return {key: values[key] for key in FEATURE_COLUMNS}


def peak(values: list[float]) -> float | None:
    return max(values, key=abs) if values else None


class FeatureComputer:
    def __init__(self, gap_s: float = GAP_S) -> None:
        self.gap_s = gap_s
        self.computed = 0
        self.skipped: Counter[str] = Counter()
        self.pairs_skipped: Counter[str] = Counter()

    def compute(
        self, icao24: str, samples: tuple[Sample, ...], gaps: RecentGaps | None = None
    ) -> TrackFeatures | None:
        if len(samples) < MIN_SAMPLES:
            self.skipped["not_ready"] += 1
            return None
        intervals = [b.last_contact - a.last_contact for a, b in pairwise(samples)]
        if any(not DT_FLOOR_S <= dt <= self.gap_s for dt in intervals):
            self.skipped["dt_invalid"] += 1
            return None
        speeds: list[float] = []
        reported: list[float] = []
        ratios: list[float] = []
        distances: list[float] = []
        turns: list[float] = []
        accelerations: list[float] = []
        verticals: list[float] = []
        reported_verticals: list[float] = []
        mismatches: list[float] = []
        previous_turn: tuple[int, float, float] | None = None
        for i, (a, b) in enumerate(pairwise(samples)):
            if a.time_position is None or b.time_position is None:
                self.pairs_skipped["no_time_position"] += 1
                continue
            dt = b.time_position - a.time_position
            if not DT_FLOOR_S <= dt <= self.gap_s:
                self.pairs_skipped["dt_invalid"] += 1
                continue
            if b.velocity is None:
                self.pairs_skipped["null_velocity"] += 1
                continue
            if a.true_track is None or b.true_track is None:
                self.pairs_skipped["null_track"] += 1
                continue
            values = (a.lat, a.lon, b.lat, b.lon, b.velocity, a.true_track, b.true_track)
            if not all(isfinite(x) for x in values):
                self.pairs_skipped["nonfinite"] += 1
                continue
            distance = haversine(a.lat, a.lon, b.lat, b.lon)
            speed = distance / dt
            speeds.append(speed)
            distances.append(distance)
            reported.append(b.velocity)
            if b.velocity >= SPEED_FLOOR_MS:
                ratios.append(speed / b.velocity)
            turn = ang_delta(b.true_track, a.true_track) / dt
            turns.append(turn)
            if previous_turn and previous_turn[0] == i - 1:
                accelerations.append((turn - previous_turn[1]) / ((dt + previous_turn[2]) / 2))
            previous_turn = (i, turn, dt)
            if b.vertical_rate is not None and isfinite(b.vertical_rate):
                reported_verticals.append(b.vertical_rate)
            if (
                a.baro_alt is not None
                and b.baro_alt is not None
                and isfinite(a.baro_alt)
                and isfinite(b.baro_alt)
            ):
                vertical = (b.baro_alt - a.baro_alt) / dt
                verticals.append(vertical)
                if b.vertical_rate is not None and isfinite(b.vertical_rate):
                    mismatches.append(abs(vertical - b.vertical_rate))
        if not speeds:
            reason = (
                "no_time_position"
                if any(s.time_position is None for s in samples)
                else "null_velocity"
                if any(s.velocity is None for s in samples)
                else "null_track"
                if any(s.true_track is None for s in samples)
                else "dt_invalid"
            )
            self.skipped[reason] += 1
            return None
        self.computed += 1
        start, end = samples[0].last_contact, samples[-1].last_contact
        return TrackFeatures(
            median(speeds),
            median(reported),
            median(ratios) if ratios else None,
            max(turns, key=abs),
            peak(accelerations),
            peak(reported_verticals),
            peak(verticals),
            peak(mismatches),
            max(distances),
            max(speeds),
            median(intervals),
            pvariance(intervals),
            icao24,
            start,
            end,
            end - start,
            len(samples),
            len(speeds),
            gaps is not None and any(gaps.overlaps(start, end, s.region) for s in samples),
            any(s.on_ground for s in samples),
            samples[-1].region,
        )
