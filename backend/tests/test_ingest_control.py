import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as main_module
from app.api.ingest_control import MAX_ERROR_CHARS, CycleRunner, router
from app.config import REGIONS, Settings, settings
from app.ingest import opensky
from app.ingest.budget import CreditBudget
from app.ingest.opensky import FetchResult, OpenSkyClient
from app.ingest.parser import parse_states

BACKEND_DIR = Path(main_module.__file__).resolve().parents[1]
REGION = REGIONS["bay_area"]


def row(icao24: str = "abc123") -> list[object]:
    return [icao24, "UAL123", "United States", 1_700_000_000, 1_700_000_001, -122.3, 37.6,
            10_000.0, False, 250.0, 180.0, -2.5, None, 10_200.0, "1200", False, 0, 3]


def success(states: list[object] | None) -> FetchResult:
    parsed = parse_states({"time": 1_700_000_000, "states": states}, REGION.name)
    return FetchResult(REGION.name, True, parsed, 100, None, 200)


def runner_for(outcome, on_result=None) -> tuple[CycleRunner, AsyncMock]:
    client = AsyncMock(spec=OpenSkyClient)
    client.fetch_region.side_effect = [outcome]
    handler = on_result or AsyncMock()
    return CycleRunner(client, [REGION], handler), handler


async def test_cycle_counts_accepted_and_rejected_rows() -> None:
    result = success([row(), ["malformed"]])
    runner, handler = runner_for(result)

    record = await runner.run("mixed")

    assert (record.status, record.scenario_id) == ("succeeded", "mixed")
    assert (record.accepted, record.rejected) == (1, 1)
    handler.assert_awaited_once_with(result)
    assert runner.get(record.cycle_id) is record


async def test_empty_snapshot_completes_successfully() -> None:
    runner, _ = runner_for(success(None))

    record = await runner.run(None)

    assert (record.status, record.accepted, record.rejected) == ("succeeded", 0, 0)


async def test_upstream_failure_finishes_as_failed_without_ingesting() -> None:
    failure = FetchResult(REGION.name, False, None, None, None, 503, "OpenSky returned HTTP 503")
    runner, handler = runner_for(failure)

    record = await runner.run(None)

    assert record.status == "failed"
    assert record.regions[0].status_code == 503
    assert record.regions[0].error == "OpenSky returned HTTP 503"
    handler.assert_not_awaited()


async def test_handler_exception_is_recorded_with_bounded_error() -> None:
    handler = AsyncMock(side_effect=ValueError("x" * (MAX_ERROR_CHARS * 2)))
    runner, _ = runner_for(success([row()]), on_result=handler)

    record = await runner.run(None)

    assert record.status == "failed"
    assert record.regions[0].error.startswith("ValueError")
    assert len(record.regions[0].error) == MAX_ERROR_CHARS


def test_cycle_endpoints_return_the_recorded_outcome() -> None:
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.cycle_runner, _ = runner_for(success([row()]))

    with TestClient(test_app) as client:
        created = client.post("/__test/ingest/cycles", json={"scenario_id": "normal"}).json()
        fetched = client.get(f"/__test/ingest/cycles/{created['cycle_id']}")
        missing = client.get("/__test/ingest/cycles/unknown")

    assert created["scenario_id"] == "normal"
    assert fetched.json() == created
    assert missing.status_code == 404


def test_test_routes_are_absent_in_normal_mode() -> None:
    assert not settings.skywatch_test_mode
    client = TestClient(main_module.app)

    responses = [client.get("/__test/ingest/cycles/any"), client.post("/__test/ingest/cycles")]

    assert [r.json() for r in responses] == [{"detail": "Not Found"}] * 2


def test_test_routes_exist_only_when_enabled() -> None:
    env = {
        **os.environ,
        "SKYWATCH_TEST_MODE": "true",
        "OPENSKY_TOKEN_URL": "http://provider-fixture:8080/token",
        "OPENSKY_STATES_URL": "http://provider-fixture:8080/states/all",
    }
    script = (
        "from unittest.mock import Mock\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "app.state.cycle_runner = Mock(get=Mock(return_value=None))\n"
        "print(TestClient(app).get('/__test/ingest/cycles/any').json()['detail'])\n"
    )

    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=60, check=True,
    )

    assert done.stdout.strip() == "Cycle not found"


@pytest.mark.parametrize(
    "overrides",
    [{}, {"opensky_token_url": "http://fixture/token"}, {"opensky_states_url": "http://fixture/s"}],
)
def test_test_mode_refuses_the_real_provider(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="local fixture"):
        Settings(_env_file=None, skywatch_test_mode=True, **overrides)


async def test_client_uses_configured_provider_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url.copy_with(query=None)))
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={"time": 1_700_000_000, "states": []})

    monkeypatch.setattr(opensky, "states_budget", CreditBudget(4000))
    monkeypatch.setattr(settings, "opensky_client_id", "dummy")
    monkeypatch.setattr(settings, "opensky_client_secret", "dummy")
    monkeypatch.setattr(settings, "opensky_token_url", "http://fixture/token")
    monkeypatch.setattr(settings, "opensky_states_url", "http://fixture/states/all")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenSkyClient(http_client=http_client)
        await client._fetch_token()
        await client._get_states("t", REGION)

    assert requested == ["http://fixture/token", "http://fixture/states/all"]


async def test_lifespan_in_test_mode_does_not_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = AsyncMock()
    queue = AsyncMock()
    queue.qsize = lambda: 0

    async def idle_writer() -> None:
        await asyncio.Event().wait()

    for name in ("connect_db", "connect_redis", "disconnect_db",
                 "disconnect_redis", "apply_migrations"):
        monkeypatch.setattr(main_module, name, AsyncMock())
    monkeypatch.setattr(main_module, "db_pool", lambda: object())
    monkeypatch.setattr(main_module, "writer_loop", idle_writer)
    monkeypatch.setattr(main_module, "write_queue", queue)
    monkeypatch.setattr(main_module.OpenSkyClient, "fetch_region", fetch)
    monkeypatch.setattr(settings, "skywatch_test_mode", True)

    test_app = FastAPI(lifespan=main_module.lifespan)

    async with test_app.router.lifespan_context(test_app):
        await asyncio.sleep(0.05)

        assert isinstance(test_app.state.cycle_runner, CycleRunner)
        assert test_app.state.region_scheduler.intervals == {}

    fetch.assert_not_awaited()
