import asyncio

import pytest

import app.ingest.scheduler as scheduler_module
from app.config import REGIONS
from app.ingest.opensky import FetchResult
from app.ingest.parser import parse_states
from app.ingest.scheduler import FETCH_FAILURE_BACKOFF_S, RegionScheduler


def make_state_row() -> list[object]:
    return [
        "abc123",
        "UAL123  ",
        "United States",
        1_700_000_000,
        1_700_000_001,
        -122.3,
        37.6,
        10_000.0,
        False,
        250.0,
        180.0,
        -2.5,
        None,
        10_200.0,
        "1200",
        False,
        0,
        3,
    ]


def success_result(
    aircraft_count: int = 1,
    credits_remaining: int = 3999,
) -> FetchResult:
    rows = [make_state_row() for _ in range(aircraft_count)]

    parsed = parse_states(
        payload={
            "time": 1_700_000_000,
            "states": rows,
        },
        region="bay_area",
    )

    return FetchResult(
        region="bay_area",
        ok=True,
        parsed=parsed,
        credits_remaining=credits_remaining,
        retry_after_s=None,
        status_code=200,
        error=None,
    )


class FakeClient:
    def __init__(self, result: FetchResult) -> None:
        self.result = result
        self.calls = 0

    async def fetch_region(self, region):
        self.calls += 1
        return self.result


async def full_budget() -> float:
    return 1.0


@pytest.mark.asyncio
async def test_successful_poll_updates_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(
        success_result(aircraft_count=0)
    )

    scheduler = RegionScheduler(
        client=client,
        regions=[REGIONS["bay_area"]],
        budget_fraction=full_budget,
    )

    slept_for: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept_for.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        scheduler_module.asyncio,
        "sleep",
        fake_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await scheduler._loop(REGIONS["bay_area"])

    assert client.calls == 1

    # Sparse Bay Area:
    # base 15 -> target 30 -> smoothed 22.5
    assert scheduler.intervals["bay_area"] == 22.5
    assert slept_for == [22.5]


@pytest.mark.asyncio
async def test_dense_airspace_tightens_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(
        success_result(aircraft_count=150)
    )

    scheduler = RegionScheduler(
        client=client,
        regions=[REGIONS["bay_area"]],
        budget_fraction=full_budget,
    )

    async def fake_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        scheduler_module.asyncio,
        "sleep",
        fake_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await scheduler._loop(REGIONS["bay_area"])

    assert scheduler.intervals["bay_area"] == 11.25


@pytest.mark.asyncio
async def test_rate_limit_uses_server_retry_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = FetchResult(
        region="bay_area",
        ok=False,
        parsed=None,
        credits_remaining=0,
        retry_after_s=120,
        status_code=429,
        error="OpenSky rate limit exceeded",
    )

    client = FakeClient(result)

    scheduler = RegionScheduler(
        client=client,
        regions=[REGIONS["bay_area"]],
        budget_fraction=full_budget,
    )

    slept_for: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept_for.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        scheduler_module.asyncio,
        "sleep",
        fake_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await scheduler._loop(REGIONS["bay_area"])

    assert client.calls == 1

    # Server controls the retry interval.
    assert slept_for == [120.0]

    # Adaptive interval must not change because a 429 tells us
    # nothing about aircraft density.
    assert scheduler.intervals["bay_area"] == 15.0


@pytest.mark.asyncio
async def test_failed_fetch_uses_failure_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = FetchResult(
        region="bay_area",
        ok=False,
        parsed=None,
        credits_remaining=3900,
        retry_after_s=None,
        status_code=500,
        error="OpenSky returned HTTP 500",
    )

    client = FakeClient(result)

    scheduler = RegionScheduler(
        client=client,
        regions=[REGIONS["bay_area"]],
        budget_fraction=full_budget,
    )

    slept_for: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept_for.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        scheduler_module.asyncio,
        "sleep",
        fake_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await scheduler._loop(REGIONS["bay_area"])

    assert slept_for == [FETCH_FAILURE_BACKOFF_S]

    # A network/server failure is NOT equivalent to seeing zero aircraft.
    assert scheduler.intervals["bay_area"] == 15.0


@pytest.mark.asyncio
async def test_successful_result_calls_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = success_result(aircraft_count=50)
    client = FakeClient(result)

    received: list[FetchResult] = []

    async def on_result(fetch_result: FetchResult) -> None:
        received.append(fetch_result)

    scheduler = RegionScheduler(
        client=client,
        regions=[REGIONS["bay_area"]],
        budget_fraction=full_budget,
        on_result=on_result,
    )

    async def fake_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        scheduler_module.asyncio,
        "sleep",
        fake_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await scheduler._loop(REGIONS["bay_area"])

    assert received == [result]