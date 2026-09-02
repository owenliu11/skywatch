import httpx
import pytest

from app.config import REGIONS
from app.ingest import opensky
from app.ingest.budget import CreditBudget
from app.ingest.opensky import OpenSkyClient


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


async def _async_value(value: str) -> str:
    return value

@pytest.fixture(autouse=True)
def fresh_credit_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give every test an independent OpenSky credit budget."""
    monkeypatch.setattr(
        opensky,
        "states_budget",
        CreditBudget(4000),
    )


@pytest.mark.asyncio
async def test_fetch_region_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if "states/all" in str(request.url):
            return httpx.Response(
                200,
                headers={
                    "X-Rate-Limit-Remaining": "3999",
                },
                json={
                    "time": 1_700_000_000,
                    "states": [make_state_row()],
                },
            )

        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = OpenSkyClient(http_client=http_client)

        monkeypatch.setattr(
            client,
            "get_token",
            lambda: _async_value("fake-token"),
        )

        result = await client.fetch_region(REGIONS["bay_area"])

    assert result.ok
    assert result.status_code == 200
    assert result.credits_remaining == 3999
    assert result.parsed is not None
    assert result.parsed.snapshot.aircraft_count == 1


@pytest.mark.asyncio
async def test_rate_limit_returns_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={
                "X-Rate-Limit-Remaining": "0",
                "X-Rate-Limit-Retry-After-Seconds": "120",
            },
        )

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = OpenSkyClient(http_client=http_client)

        monkeypatch.setattr(
            client,
            "get_token",
            lambda: _async_value("fake-token"),
        )

        result = await client.fetch_region(REGIONS["bay_area"])

    assert not result.ok
    assert result.status_code == 429
    assert result.retry_after_s == 120
    assert result.credits_remaining == 0


@pytest.mark.asyncio
async def test_server_error_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = OpenSkyClient(http_client=http_client)

        monkeypatch.setattr(
            client,
            "get_token",
            lambda: _async_value("fake-token"),
        )

        result = await client.fetch_region(REGIONS["bay_area"])

    assert not result.ok
    assert result.status_code == 500
    assert result.parsed is None


@pytest.mark.asyncio
async def test_malformed_response_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "states": [],
            },
        )

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = OpenSkyClient(http_client=http_client)

        monkeypatch.setattr(
            client,
            "get_token",
            lambda: _async_value("fake-token"),
        )

        result = await client.fetch_region(REGIONS["bay_area"])

    assert not result.ok
    assert result.parsed is None
    assert result.error is not None
    assert "invalid OpenSky response" in result.error

@pytest.mark.asyncio
async def test_401_refreshes_token_and_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_count = 0
    tokens = iter(["old-token", "new-token"])
    invalidation_count = 0

    async def fake_get_token() -> str:
        return next(tokens)

    async def fake_invalidate_token() -> None:
        nonlocal invalidation_count
        invalidation_count += 1

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1

        authorization = request.headers.get("Authorization")

        if request_count == 1:
            assert authorization == "Bearer old-token"
            return httpx.Response(401)

        assert authorization == "Bearer new-token"

        return httpx.Response(
            200,
            headers={
                "X-Rate-Limit-Remaining": "3999",
            },
            json={
                "time": 1_700_000_000,
                "states": [make_state_row()],
            },
        )

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = OpenSkyClient(http_client=http_client)

        monkeypatch.setattr(
            client,
            "get_token",
            fake_get_token,
        )

        monkeypatch.setattr(
            client,
            "invalidate_token",
            fake_invalidate_token,
        )

        result = await client.fetch_region(REGIONS["bay_area"])

    assert result.ok
    assert result.status_code == 200
    assert result.parsed is not None

    assert request_count == 2
    assert invalidation_count == 1

@pytest.mark.asyncio
async def test_second_401_stops_after_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_count = 0
    token_count = 0
    invalidation_count = 0

    async def fake_get_token() -> str:
        nonlocal token_count
        token_count += 1
        return f"token-{token_count}"

    async def fake_invalidate_token() -> None:
        nonlocal invalidation_count
        invalidation_count += 1

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(401)

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = OpenSkyClient(http_client=http_client)

        monkeypatch.setattr(
            client,
            "get_token",
            fake_get_token,
        )

        monkeypatch.setattr(
            client,
            "invalidate_token",
            fake_invalidate_token,
        )

        result = await client.fetch_region(REGIONS["bay_area"])

    assert not result.ok
    assert result.status_code == 401
    assert result.parsed is None

    assert request_count == 2
    assert token_count == 2
    assert invalidation_count == 2

    assert result.error is not None
    assert "authentication failed" in result.error

@pytest.mark.asyncio
async def test_network_timeout_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(
            "simulated timeout",
            request=request,
        )

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = OpenSkyClient(http_client=http_client)

        monkeypatch.setattr(
            client,
            "get_token",
            lambda: _async_value("fake-token"),
        )

        result = await client.fetch_region(REGIONS["bay_area"])

    assert not result.ok
    assert result.status_code is None
    assert result.parsed is None
    assert result.error is not None
    assert "ReadTimeout" in result.error