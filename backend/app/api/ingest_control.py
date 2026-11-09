"""Test-only control of ingestion. Registered only when SKYWATCH_TEST_MODE is true.

A cycle runs the same client and ingest handler as continuous polling, inside the
API process that owns LiveState. A finished cycle means ingest handling completed;
history is written asynchronously, so persistence must be confirmed separately.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.config import Region
from app.ingest.opensky import FetchResult, OpenSkyClient

log = logging.getLogger(__name__)

MAX_RETAINED_CYCLES = 256
MAX_ERROR_CHARS = 500

router = APIRouter(prefix="/__test", include_in_schema=False)


class CycleRequest(BaseModel):
    scenario_id: str | None = None


@dataclass(frozen=True)
class RegionOutcome:
    region: str
    ok: bool
    status_code: int | None
    accepted: int
    rejected: int
    error: str | None


@dataclass(frozen=True)
class CycleRecord:
    cycle_id: str
    scenario_id: str | None
    status: str
    started_at: str
    finished_at: str
    accepted: int
    rejected: int
    regions: tuple[RegionOutcome, ...]


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]


class CycleRunner:
    """Run single ingestion cycles and retain their outcomes."""

    def __init__(
        self,
        client: OpenSkyClient,
        regions: list[Region],
        on_result: Callable[[FetchResult], Awaitable[None]],
    ) -> None:
        self._client = client
        self._regions = regions
        self._on_result = on_result
        self._lock = asyncio.Lock()
        self._cycles: dict[str, CycleRecord] = {}

    async def run(self, scenario_id: str | None) -> CycleRecord:
        async with self._lock:
            started_at = datetime.now(UTC).isoformat()
            regions = tuple([await self._run_region(region) for region in self._regions])
            record = CycleRecord(
                cycle_id=uuid.uuid4().hex,
                scenario_id=scenario_id,
                status="succeeded" if all(r.ok for r in regions) else "failed",
                started_at=started_at,
                finished_at=datetime.now(UTC).isoformat(),
                accepted=sum(r.accepted for r in regions),
                rejected=sum(r.rejected for r in regions),
                regions=regions,
            )
            self._cycles[record.cycle_id] = record
            if len(self._cycles) > MAX_RETAINED_CYCLES:
                del self._cycles[next(iter(self._cycles))]
            return record

    def get(self, cycle_id: str) -> CycleRecord | None:
        return self._cycles.get(cycle_id)

    async def _run_region(self, region: Region) -> RegionOutcome:
        status_code = None
        accepted = rejected = 0
        try:
            result = await self._client.fetch_region(region)
            status_code = result.status_code
            if result.parsed is not None:
                accepted = result.parsed.snapshot.aircraft_count
                rejected = result.parsed.error_count
            if result.ok:
                await self._on_result(result)
            error = result.error[:MAX_ERROR_CHARS] if result.error else None
            return RegionOutcome(region.name, result.ok, status_code, accepted, rejected, error)
        except Exception as exc:
            log.exception("%s: test ingestion cycle failed", region.name)
            return RegionOutcome(region.name, False, status_code, 0, rejected, _describe(exc))


@router.post("/ingest/cycles")
async def run_cycle(
    request: Request,
    body: CycleRequest | None = None,
) -> dict[str, object]:
    """Run one cycle and return its outcome once ingest handling has finished."""

    runner: CycleRunner = request.app.state.cycle_runner
    return asdict(await runner.run(body.scenario_id if body else None))


@router.get("/ingest/cycles/{cycle_id}")
async def get_cycle(
    request: Request,
    cycle_id: str,
) -> dict[str, object]:
    runner: CycleRunner = request.app.state.cycle_runner
    record = runner.get(cycle_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Cycle not found")
    return asdict(record)
