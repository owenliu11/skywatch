"""Ordered, mutually exclusive rules. No trained model."""

from collections import Counter

from app.detect.features import SPEED_FLOOR_MS, TrackFeatures


class RuleDetector:
    def __init__(self) -> None:
        self.abstentions: Counter[str] = Counter()
        self.detections: Counter[str] = Counter()

    def evaluate(self, f: TrackFeatures | None) -> str | None:
        if f is None:
            self.abstentions["uncomputable"] += 1
            return None
        if f.on_ground_any or f.spans_gap:
            self.abstentions["on_ground" if f.on_ground_any else "ingest_gap"] += 1
            return None
        name = None
        if f.implied_speed_ms > 617:
            name = "impossible_speed"
        elif f.jump_speed_ms > 617 and f.update_interval_s < 60:
            name = "teleport"
        elif f.implied_vertical_rate_ms is not None and abs(f.implied_vertical_rate_ms) > 40.6:
            name = "impossible_climb"
        # Track C: near-stationary heading changes are not reliable turn evidence.
        elif f.reported_speed_ms >= SPEED_FLOOR_MS and abs(f.turn_rate_deg_s) > 15:
            name = "impossible_turn"
        elif f.speed_mismatch_ratio is not None and (
            f.speed_mismatch_ratio > 3 or f.speed_mismatch_ratio < 0.33
        ):
            name = "speed_mismatch"
        if name:
            self.detections[name] += 1
            return "rule:" + name
        return None
