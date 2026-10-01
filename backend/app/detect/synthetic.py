"""Seeded great-circle tracks for controlled experiments and Phase 5 load tests."""

from collections.abc import Callable
from dataclasses import replace
from math import asin, atan2, cos, degrees, radians, sin
from random import Random

from app.detect.features import EARTH_RADIUS_M
from app.detect.windows import Sample


def destination(
    lat: float, lon: float, heading: float, distance: float
) -> tuple[float, float, float]:
    a, b, h, d = radians(lat), radians(lon), radians(heading), distance / EARTH_RADIUS_M
    c = asin(sin(a) * cos(d) + cos(a) * sin(d) * cos(h))
    e = b + atan2(sin(h) * sin(d) * cos(a), cos(d) - sin(a) * sin(c))
    reverse = atan2(sin(b - e) * cos(a), cos(c) * sin(a) - sin(c) * cos(a) * cos(b - e))
    return degrees(c), (degrees(e) + 180) % 360 - 180, (degrees(reverse) + 180) % 360


def generate_track(
    *,
    duration_s: float,
    n_aircraft: int = 1,
    seed: int,
    interval_dist: Callable[[], float],
    null_rates: dict[str, float],
    start_time: float = 1_800_000_000,
) -> list[list[Sample]]:
    rng = Random(seed)
    output = []
    allowed = {"time_position", "baro_alt", "geo_alt", "velocity", "true_track", "vertical_rate"}
    if not set(null_rates) <= allowed or any(not 0 <= x <= 1 for x in null_rates.values()):
        raise ValueError("Unknown nullable field or invalid null probability")
    for _ in range(n_aircraft):
        t = 0.0
        lat, lon, heading = rng.uniform(34, 38), rng.uniform(-123, -118), rng.uniform(0, 360)
        speed, alt, vr = rng.uniform(150, 260), rng.uniform(7000, 11000), rng.uniform(-3, 3)
        samples = []
        while t <= duration_s:
            sample = Sample(
                start_time + t,
                start_time + t,
                lat,
                lon,
                alt,
                alt + 50,
                speed,
                heading,
                vr,
                False,
                "synthetic",
            )
            missing = {key for key, rate in null_rates.items() if rng.random() < rate}
            samples.append(
                replace(
                    sample,
                    time_position=None if "time_position" in missing else sample.time_position,
                    baro_alt=None if "baro_alt" in missing else sample.baro_alt,
                    geo_alt=None if "geo_alt" in missing else sample.geo_alt,
                    velocity=None if "velocity" in missing else sample.velocity,
                    true_track=None if "true_track" in missing else sample.true_track,
                    vertical_rate=None if "vertical_rate" in missing else sample.vertical_rate,
                )
            )
            dt = float(interval_dist())
            if not 0 < dt < float("inf"):
                raise ValueError("Report intervals must be finite and positive")
            lat, lon, heading = destination(lat, lon, heading, speed * dt)
            alt += vr * dt
            t += dt
        output.append(samples)
    return output
