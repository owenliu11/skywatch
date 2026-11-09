"""Copy-on-write corruption with explicit inclusive index labels."""

from dataclasses import dataclass, replace
from itertools import pairwise
from math import isfinite

from app.detect.synthetic import destination
from app.detect.windows import Sample


@dataclass(frozen=True)
class InjectionLabel:
    kind: str
    start: int
    end: int
    magnitude: float
    perturbed: tuple[str, ...]


class AnomalyInjector:
    def inject(
        self,
        track: list[Sample],
        kind: str,
        start: int,
        end: int,
        magnitude: float,
        donor: list[Sample] | None = None,
    ) -> tuple[list[Sample], InjectionLabel]:
        if not 0 < start <= end < len(track):
            raise ValueError(
                "Injection requires a preceding clean sample and valid inclusive range"
            )
        if not isfinite(magnitude):
            raise ValueError("Injection magnitude must be finite")
        if kind == "frozen_position" and any(
            a.time_position is None or b.time_position is None or b.time_position <= a.time_position
            for a, b in pairwise(track[start - 1 : end + 1])
        ):
            raise ValueError("Frozen-position injection requires advancing position timestamps")
        if kind == "slow_drift" and any(s.true_track is None for s in track[start : end + 1]):
            raise ValueError("Lateral drift requires reported headings")
        output = list(track)
        fields: tuple[str, ...]
        if kind == "duplicate_identity":
            if not donor:
                raise ValueError("A second coherent track is required")
            t0, t1 = track[start].last_contact, track[end].last_contact
            additions = [s for s in donor if t0 <= s.last_contact <= t1]
            if not additions:
                raise ValueError("Donor must overlap the injection interval")
            if {s.last_contact for s in additions} & {s.last_contact for s in track}:
                raise ValueError("Donor timestamps must interleave, not duplicate reports")
            output = sorted(track + additions, key=lambda s: s.last_contact)
            indices = [i for i, s in enumerate(output) if t0 <= s.last_contact <= t1]
            return output, InjectionLabel(
                kind, min(indices), max(indices), magnitude, ("identity",)
            )
        if kind == "teleport":
            end = start
            fields = ("lat", "lon")
        elif kind == "velocity_inconsistency":
            fields = ("velocity",)
        elif kind in ("impossible_climb_a", "impossible_climb_b"):
            fields = ("baro_alt", "geo_alt") + (("vertical_rate",) if kind.endswith("_b") else ())
        elif kind in ("frozen_position", "slow_drift"):
            fields = ("lat", "lon")
        else:
            raise ValueError("Unknown injection type: " + kind)
        base = track[start - 1]
        for i in range(start, end + 1):
            s = track[i]
            if kind == "teleport":
                lat, lon, _ = destination(s.lat, s.lon, 90, magnitude)
                output[i] = replace(s, lat=lat, lon=lon)
            elif kind == "velocity_inconsistency":
                output[i] = replace(
                    s, velocity=None if s.velocity is None else s.velocity * magnitude
                )
            elif kind.startswith("impossible_climb"):
                if s.time_position is None or base.time_position is None:
                    raise ValueError("Climb injection requires position timestamps")
                offset = magnitude * (s.time_position - base.time_position)
                output[i] = replace(
                    s,
                    baro_alt=None if s.baro_alt is None else s.baro_alt + offset,
                    geo_alt=None if s.geo_alt is None else s.geo_alt + offset,
                    vertical_rate=(None if s.vertical_rate is None else s.vertical_rate + magnitude)
                    if kind.endswith("_b")
                    else s.vertical_rate,
                )
            elif kind == "frozen_position":
                output[i] = replace(s, lat=base.lat, lon=base.lon)
            else:
                assert s.true_track is not None
                # magnitude is fabricated lateral motion in m/s, not degrees/minute.
                lat, lon, _ = destination(
                    s.lat,
                    s.lon,
                    s.true_track + 90,
                    magnitude * (s.last_contact - base.last_contact),
                )
                output[i] = replace(s, lat=lat, lon=lon)
        # A finite ramp must remain continuous after the event: retain the final
        # offset, avoiding an unlabeled return jump at end+1.
        if kind.startswith("impossible_climb") or kind == "slow_drift":
            for i in range(end + 1, len(output)):
                s = track[i]
                if kind.startswith("impossible_climb"):
                    end_position = track[end].time_position
                    assert end_position is not None and base.time_position is not None
                    delta = magnitude * (end_position - base.time_position)
                    geo_delta = delta
                    output[i] = replace(
                        s,
                        baro_alt=None if s.baro_alt is None else s.baro_alt + delta,
                        geo_alt=None if s.geo_alt is None else s.geo_alt + geo_delta,
                    )
                else:
                    end_heading = track[end].true_track
                    assert end_heading is not None
                    lat, lon, _ = destination(
                        s.lat,
                        s.lon,
                        end_heading + 90,
                        magnitude * (track[end].last_contact - base.last_contact),
                    )
                    output[i] = replace(s, lat=lat, lon=lon)
        # The return from a frozen position is also part of the injected event.
        if kind == "frozen_position":
            end = min(end + 1, len(track) - 1)
        return output, InjectionLabel(kind, start, end, magnitude, fields)
