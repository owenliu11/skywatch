import asyncio

import pytest

import app.ingest.opensky as opensky_module
from app.ingest.opensky import OpenSkyClient


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.locks: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ):
        if nx:
            if key in self.data:
                return False

            self.data[key] = value
            return True

        self.data[key] = value
        return True

    async def delete(self, key: str):
        self.data.pop(key, None)

    async def eval(
        self,
        script: str,
        numkeys: int,
        key: str,
        lock_value: str,
    ):
        if self.data.get(key) == lock_value:
            self.data.pop(key, None)
            return 1

        return 0


@pytest.mark.asyncio
async def test_concurrent_callers_fetch_token_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    client = OpenSkyClient()

    fetch_count = 0

    async def fake_fetch_token() -> tuple[str, int]:
        nonlocal fetch_count

        fetch_count += 1

        # Give the other tasks time to arrive while this
        # caller owns the single-flight lock.
        await asyncio.sleep(0.05)

        return "shared-token", 1800

    monkeypatch.setattr(
        opensky_module,
        "get_redis",
        lambda: redis,
    )

    monkeypatch.setattr(
        client,
        "_fetch_token",
        fake_fetch_token,
    )

    results = await asyncio.gather(
        *[
            client.get_token()
            for _ in range(10)
        ]
    )

    assert results == ["shared-token"] * 10
    assert fetch_count == 1

    assert redis.data[opensky_module.TOKEN_KEY] == "shared-token"

    # Lock should be released after refresh completes.
    assert opensky_module.TOKEN_LOCK_KEY not in redis.data

@pytest.mark.asyncio
async def test_failed_refresh_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    client = OpenSkyClient()

    attempts = 0

    async def fake_fetch_token() -> tuple[str, int]:
        nonlocal attempts

        attempts += 1

        if attempts == 1:
            raise RuntimeError("simulated OAuth failure")

        return "recovered-token", 1800

    monkeypatch.setattr(
        opensky_module,
        "get_redis",
        lambda: redis,
    )

    monkeypatch.setattr(
        client,
        "_fetch_token",
        fake_fetch_token,
    )

    with pytest.raises(
        RuntimeError,
        match="simulated OAuth failure",
    ):
        await client.get_token()

    # Even though refresh failed, finally must release the lock.
    assert opensky_module.TOKEN_LOCK_KEY not in redis.data

    # A later caller must be able to acquire the lock and recover.
    token = await client.get_token()

    assert token == "recovered-token"
    assert attempts == 2

    assert redis.data[opensky_module.TOKEN_KEY] == "recovered-token"
    assert opensky_module.TOKEN_LOCK_KEY not in redis.data