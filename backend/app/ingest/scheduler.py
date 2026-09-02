"""Adaptive polling scheduler.

The polling policy lives in `next_interval`, which is intentionally pure:
no clock, Redis, HTTP, or asyncio. The asynchronous scheduler only coordinates
fetches, retry behavior, callbacks, and sleep intervals.

Keeping policy separate from orchestration makes the interval logic easy to
unit-test and prevents network failures from being confused with sparse
airspace.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.config import MAX_POLL_INTERVAL_S, MIN_POLL_INTERVAL_S, Region
from app.ingest.opensky import FetchResult, OpenSkyClient

log = logging.getLogger(__name__)

DENSE_AIRCRAFT = 120
SPARSE_AIRCRAFT = 20

BUDGET_PRESSURE_THRESHOLD = 0.20
MAX_WIDEN_FACTOR = 6.0

SMOOTHING = 0.5

FETCH_FAILURE_BACKOFF_S = 30.0


def next_interval(
    current_s: float,
    aircraft_seen: int,
    budget_fraction: float,
    region: Region,
) -> float:
    """Return the next successful-poll interval for one region.

    This function is pure: the same inputs always produce the same output.

    Density controls sampling frequency, while remaining credit budget can
    progressively widen the interval as the daily quota becomes scarce.
    """
    budget_fraction = max(0.0, min(1.0, budget_fraction))

    base = region.base_interval_s

    # 1. Aircraft density.
    #
    # Dense airspace benefits from more frequent observations.
    # Sparse airspace can be sampled less aggressively.
    if aircraft_seen >= DENSE_AIRCRAFT:
        target = base * 0.5
    elif aircraft_seen <= SPARSE_AIRCRAFT:
        target = base * 2.0
    else:
        target = base

    # 2. Credit-budget pressure.
    #
    # Below the threshold, widen progressively rather than consuming the
    # remaining quota at the normal rate and going completely dark later.
    if budget_fraction < BUDGET_PRESSURE_THRESHOLD:
        safe_fraction = max(budget_fraction, 0.01)

        widen_factor = min(
            BUDGET_PRESSURE_THRESHOLD / safe_fraction,
            MAX_WIDEN_FACTOR,
        )

        target *= widen_factor

    # 3. Smooth changes to avoid interval flapping.
    smoothed = current_s + SMOOTHING * (target - current_s)

    # 4. Respect global scheduler bounds.
    return max(
        MIN_POLL_INTERVAL_S,
        min(MAX_POLL_INTERVAL_S, smoothed),
    )


class RegionScheduler:
    """Run one adaptive OpenSky polling loop per configured region."""

    def __init__(
        self,
        client: OpenSkyClient,
        regions: list[Region],
        budget_fraction: Callable[[], Awaitable[float]],
        on_result: Callable[[FetchResult], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._regions = regions
        self._budget_fraction = budget_fraction
        self._on_result = on_result

        self.intervals: dict[str, float] = {
            region.name: region.base_interval_s
            for region in regions
        }

        self._tasks: list[asyncio.Task[None]] = []

    async def run(self) -> None:
        """Start one polling task per region and wait for them."""
        self._tasks = [
            asyncio.create_task(
                self._loop(region),
                name=f"poll:{region.name}",
            )
            for region in self._regions
        ]

        await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        """Cancel all active region polling tasks."""
        for task in self._tasks:
            task.cancel()

        await asyncio.gather(
            *self._tasks,
            return_exceptions=True,
        )

        self._tasks.clear()

    async def _loop(self, region: Region) -> None:
        """Continuously poll one region."""
        while True:
            try:
                result = await self._client.fetch_region(region)

            except asyncio.CancelledError:
                # Cancellation is how stop() shuts the scheduler down.
                # Never swallow it as a normal fetch failure.
                raise

            except Exception:
                # This is a final safety boundary for the region task.
                #
                # OpenSkyClient should normally convert expected HTTP/network
                # failures into FetchResult values, but an unexpected exception
                # must not permanently kill this region's polling loop.
                log.exception(
                    "%s: unexpected polling failure; retrying in %.0fs",
                    region.name,
                    FETCH_FAILURE_BACKOFF_S,
                )

                await asyncio.sleep(FETCH_FAILURE_BACKOFF_S)
                continue

            # Server-directed rate-limit backoff is different from adaptive
            # polling. Honor it exactly and do not feed it into density logic.
            if result.retry_after_s is not None:
                retry_s = max(
                    MIN_POLL_INTERVAL_S,
                    float(result.retry_after_s),
                )

                log.info(
                    "%s: server requested %.0fs retry delay",
                    region.name,
                    retry_s,
                )

                await asyncio.sleep(retry_s)
                continue

            if self._on_result is not None:
                await self._on_result(result)

            # A failed request tells us nothing about aircraft density.
            #
            # Do NOT translate failure into aircraft_seen=0, because that would
            # incorrectly teach the adaptive policy that the region is sparse.
            if not result.ok:
                log.warning(
                    "%s: fetch failed; retrying in %.0fs",
                    region.name,
                    FETCH_FAILURE_BACKOFF_S,
                )

                await asyncio.sleep(FETCH_FAILURE_BACKOFF_S)
                continue

            aircraft_seen = result.parsed.snapshot.aircraft_count

            previous = self.intervals[region.name]

            budget_fraction = await self._budget_fraction()

            updated = next_interval(
                current_s=previous,
                aircraft_seen=aircraft_seen,
                budget_fraction=budget_fraction,
                region=region,
            )

            self.intervals[region.name] = updated

            if abs(updated - previous) > 0.5:
                direction = (
                    "widened"
                    if updated > previous
                    else "tightened"
                )

                credits = (
                    str(result.credits_remaining)
                    if result.credits_remaining is not None
                    else "unknown"
                )

                log.info(
                    "%s %s %.1fs -> %.1fs, aircraft=%d, credits=%s",
                    direction,
                    region.name,
                    previous,
                    updated,
                    aircraft_seen,
                    credits,
                )

            await asyncio.sleep(updated)