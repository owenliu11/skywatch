"""One synchronous scoring pass per broadcast tick; persistence in its own task."""

import asyncio
import logging
from collections import deque
from math import ceil
from time import perf_counter

import asyncpg

from app.db import pool as db_pool
from app.detect.features import FeatureComputer
from app.detect.gaps import GAP_RETENTION_S, RecentGaps
from app.detect.rules import RuleDetector
from app.detect.store import AnomalyStore
from app.detect.windows import TrackWindows

log = logging.getLogger(__name__)


class DetectionPipeline:
    def __init__(self, windows: TrackWindows, gaps: RecentGaps) -> None:
        self.windows = windows
        self.gaps = gaps
        self.features = FeatureComputer(windows.gap_s)
        self.rules = RuleDetector()
        self.store = AnomalyStore()
        self.scored_at: dict[str, float] = {}
        self.timings: deque[float] = deque(maxlen=300)

    def tick(self, now: float, cutoff: float) -> list[dict[str, object]]:
        started = perf_counter()
        self.windows.evict_before(cutoff)
        self.gaps.evict_before(now - GAP_RETENTION_S)
        windows = self.windows.snapshot()
        self.scored_at = {key: t for key, t in self.scored_at.items() if key in windows}
        opened = []
        for icao24, samples in windows.items():
            if not self.windows.is_ready(icao24):
                continue
            end = samples[-1].last_contact
            if end <= self.scored_at.get(icao24, float("-inf")):
                continue
            self.scored_at[icao24] = end
            features = self.features.compute(icao24, samples, self.gaps)
            detector = self.rules.evaluate(features)
            if detector and features:
                alert = self.store.hit(detector, features, now)
                if alert:
                    opened.append(alert.wire())
        self.store.close_expired(now)
        self.timings.append((perf_counter() - started) * 1000)
        return opened

    def health(self) -> dict[str, object]:
        timings = sorted(self.timings)
        return {
            "detector_windows": len(self.windows),
            "detector_ready": self.windows.ready_count,
            "features_computed_total": self.features.computed,
            "features_skipped_total": dict(self.features.skipped),
            "feature_pairs_skipped_total": dict(self.features.pairs_skipped),
            "detector_abstentions_total": dict(self.rules.abstentions),
            "detections_total": dict(self.rules.detections),
            "anomalies_open": len(self.store.open),
            "anomalies_pending": len(self.store.pending),
            "anomalies_flushed_total": self.store.flushed_total,
            "anomaly_flush_errors_total": self.store.flush_errors,
            "detect_ms_last": self.timings[-1] if self.timings else 0,
            "detect_ms_p95": timings[ceil(len(timings) * 0.95) - 1] if timings else 0,
            "model_version": None,
        }

    async def persist(self) -> None:
        if not self.store.pending:
            return
        try:
            async with db_pool().acquire() as connection:
                await self.store.flush(connection)
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            self.store.flush_errors += 1
            log.warning("Anomaly persistence unavailable; closed alerts retained for retry")

    async def flush_loop(self) -> None:
        while True:
            await self.persist()
            await asyncio.sleep(1)
