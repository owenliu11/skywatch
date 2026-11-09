"""Open alerts merge in memory; closed alerts persist with idempotent INSERTs."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg

from app.detect.features import TrackFeatures

COOLDOWN_S = 120.0


@dataclass
class OpenAnomaly:
    event_id: str
    icao24: str
    detector: str
    window_start: float
    window_end: float
    hit_count: int
    peak_score: float | None
    features: dict[str, object]
    region: str
    detected_at: float
    is_synthetic: bool = False

    def wire(self) -> dict[str, object]:
        row = asdict(self)
        row["score"] = row.pop("peak_score")
        return row


def severity(detector: str, f: TrackFeatures) -> float:
    """Dimensionless threshold exceedance, for selecting an alert's peak evidence."""
    if detector == "rule:impossible_speed":
        return f.implied_speed_ms / 617
    if detector == "rule:teleport":
        return f.jump_speed_ms / 617
    if detector == "rule:impossible_climb":
        return abs(f.implied_vertical_rate_ms or 0) / 40.6
    if detector == "rule:impossible_turn":
        return abs(f.turn_rate_deg_s) / 15
    ratio = f.speed_mismatch_ratio
    return max(ratio / 3, 0.33 / max(ratio, 1e-9)) if ratio is not None else 0


class AnomalyStore:
    def __init__(self) -> None:
        self.open: dict[tuple[str, str], OpenAnomaly] = {}
        self.pending: dict[str, OpenAnomaly] = {}
        self.flushed_total = 0
        self.flush_errors = 0

    def hit(self, detector: str, f: TrackFeatures, now: float) -> OpenAnomaly | None:
        key = (f.icao24, detector)
        current = self.open.get(key)
        if current and f.window_end - current.window_end > COOLDOWN_S:
            self.pending[current.event_id] = self.open.pop(key)
            current = None
        score = severity(detector, f)
        if current:
            if f.window_end <= current.window_end:
                return None
            current.window_end = f.window_end
            current.hit_count += 1
            if current.peak_score is None or score > current.peak_score:
                current.peak_score = score
                current.features = asdict(f)
            return None
        current = OpenAnomaly(
            str(uuid4()),
            f.icao24,
            detector,
            f.window_start,
            f.window_end,
            1,
            score,
            asdict(f),
            f.region,
            now,
        )
        self.open[key] = current
        return current

    def close_expired(self, now: float, *, all_open: bool = False) -> None:
        for key, current in list(self.open.items()):
            if all_open or now - current.window_end > COOLDOWN_S:
                self.pending[current.event_id] = self.open.pop(key)

    def rows(self) -> list[dict[str, object]]:
        return [item.wire() for item in (*self.open.values(), *self.pending.values())]

    async def flush(self, connection: asyncpg.Connection) -> None:
        for event_id, item in list(self.pending.items()):
            await connection.execute(
                """
                INSERT INTO anomalies (event_id,icao24,detector,window_start,window_end,
                    hit_count,score,features,region,detected_at,is_synthetic)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11)
                ON CONFLICT (event_id) DO NOTHING
            """,
                event_id,
                item.icao24,
                item.detector,
                datetime.fromtimestamp(item.window_start, UTC),
                datetime.fromtimestamp(item.window_end, UTC),
                item.hit_count,
                item.peak_score,
                json.dumps(item.features, allow_nan=False),
                item.region,
                datetime.fromtimestamp(item.detected_at, UTC),
                item.is_synthetic,
            )
            self.pending.pop(event_id, None)
            self.flushed_total += 1
