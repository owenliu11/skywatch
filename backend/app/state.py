# Phase 3 freshness thresholds.
#
# Measured from recent SkyWatch state-vector update intervals:
# p50=9s, p90=17s, p99=29s.
#
# LIVE is rounded slightly above p90.
# LOST uses roughly 2x p99 to avoid aircraft churn from normal tail latency.

"""In-memory current aircraft state for SkyWatch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.models import StateVector

STALE_AFTER_S = 20.0
LOST_AFTER_S = 60.0

@dataclass(slots=True, frozen=True)
class LiveRecord:
    """Latest known state for one aircraft."""

    state: StateVector
    region: str
    received_at: datetime


class LiveState:
    """Maintain the newest StateVector observed for each ICAO24."""

    def __init__(self) -> None:
        self._by_icao: dict[str, LiveRecord] = {}
        self.evicted_total = 0

    def upsert(
        self,
        state: StateVector,
        region: str,
    ) -> bool:
        """Insert a newer state.

        Returns True when the map changed.

        Older or duplicate observations are rejected so overlapping
        regions or delayed responses cannot make an aircraft move
        backwards in time.
        """
        last_contact = state.last_contact

        if last_contact is None:
            raise ValueError(
                f"last_contact unexpectedly missing for {state.icao24}"
            )

        existing = self._by_icao.get(state.icao24)

        if existing is not None:
            existing_last_contact = existing.state.last_contact

            if existing_last_contact is None:
                raise ValueError(
                    "stored LiveRecord unexpectedly has no last_contact"
                )

            if last_contact <= existing_last_contact:
                return False

        self._by_icao[state.icao24] = LiveRecord(
            state=state,
            region=region,
            received_at=datetime.now(UTC),
        )

        return True

    def evict_before(self, cutoff: datetime) -> int:
        """Remove aircraft whose last_contact is older than cutoff."""

        stale_icao24: list[str] = []

        for icao24, record in self._by_icao.items():
            last_contact = record.state.last_contact

            if last_contact is None:
                stale_icao24.append(icao24)
                continue

            observed_at = datetime.fromtimestamp(
                last_contact,
                tz=UTC,
            )

            if observed_at < cutoff:
                stale_icao24.append(icao24)

        for icao24 in stale_icao24:
            del self._by_icao[icao24]

        self.evicted_total += len(stale_icao24)
        return len(stale_icao24)

    def snapshot(self) -> dict[str, LiveRecord]:
        """Return a shallow snapshot of the current aircraft map."""

        return self._by_icao.copy()

    def __len__(self) -> int:
        """Return the number of currently tracked aircraft."""

        return len(self._by_icao)
    
    def get(self, icao24: str) -> LiveRecord | None:
        """Return the current record for one aircraft."""

        return self._by_icao.get(icao24)