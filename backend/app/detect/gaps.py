"""Region-aware recent ingest intervals, independent of database availability."""

GAP_RETENTION_S = 600.0


class RecentGaps:
    def __init__(self) -> None:
        self.intervals: dict[str, list[tuple[float, float]]] = {}

    def record(self, region: str, t0: float, t1: float | None = None) -> None:
        end = t0 if t1 is None else t1
        intervals = self.intervals.setdefault(region, [])
        intervals.append((min(t0, end), max(t0, end)))
        intervals.sort()
        merged: list[tuple[float, float]] = []
        for start, stop in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(stop, merged[-1][1]))
            else:
                merged.append((start, stop))
        self.intervals[region] = merged

    def overlaps(self, t0: float, t1: float, region: str | None = None) -> bool:
        groups = self.intervals.values() if region is None else [self.intervals.get(region, [])]
        return any(a <= t1 and b >= t0 for group in groups for a, b in group)

    def evict_before(self, cutoff: float) -> None:
        self.intervals = {
            region: kept
            for region, items in self.intervals.items()
            if (kept := [(a, b) for a, b in items if b >= cutoff])
        }


recent_gaps = RecentGaps()
