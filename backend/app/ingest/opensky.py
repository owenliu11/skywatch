"""Async OpenSky REST client.

This module owns:
- OAuth2 token retrieval and Redis token caching
- /states/all requests
- rate-limit metadata
- local/server credit-budget tracking
- parsing raw state vectors into domain models

The scheduler should not need to understand HTTP, OAuth, or OpenSky's raw
response format. It asks for a region and receives a FetchResult.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from redis.exceptions import RedisError

from app.config import Region, settings
from app.ingest.budget import CreditBudget, bbox_cost
from app.ingest.parser import ParseResult, parse_states
from app.redis_client import get_redis

TOKEN_URL = (
    "https://auth.opensky-network.org/"
    "auth/realms/opensky-network/protocol/openid-connect/token"
)

STATES_URL = "https://opensky-network.org/api/states/all"

TOKEN_KEY = "opensky:access_token"
TOKEN_REFRESH_MARGIN_S = 300

TOKEN_LOCK_KEY = "opensky:token_refresh_lock"
TOKEN_LOCK_TTL_S = 30
TOKEN_WAIT_INTERVAL_S = 0.1
TOKEN_WAIT_TIMEOUT_S = 10.0

states_budget = CreditBudget(settings.daily_credit_budget)


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one OpenSky region fetch."""

    region: str
    ok: bool
    parsed: ParseResult | None
    credits_remaining: int | None
    retry_after_s: int | None
    status_code: int | None
    error: str | None = None


class OpenSkyClient:
    """Async client for OpenSky state-vector ingestion."""

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._external_client = http_client
    
    async def _release_token_lock(
            self,
            *,
            redis,
            lock_value: str,
    ) -> None:
            script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            await redis.eval(
                script,
                1,
                TOKEN_LOCK_KEY,
                lock_value,
            )


    async def fetch_region(self, region: Region) -> FetchResult:
        """Fetch and parse one configured region."""

        cost = bbox_cost(
            lamin=region.lamin,
            lomin=region.lomin,
            lamax=region.lamax,
            lomax=region.lomax,
        )

        if not states_budget.can_afford(cost):
            return self._failure(
                region=region,
                error="local OpenSky credit budget exhausted",
                credits_remaining=states_budget.remaining(),
            )

        try:
            token = await self.get_token()

            response = await self._get_states(
                token=token,
                region=region,
            )

            # Cached token may expire between retrieval and request.
            # Refresh once rather than making the scheduler wait for its
            # normal failure-backoff interval.
            if response.status_code == 401:
                await self.invalidate_token()

                token = await self.get_token()

                response = await self._get_states(
                    token=token,
                    region=region,
                )

        except httpx.HTTPError as exc:
            return self._failure(
                region=region,
                error=f"{type(exc).__name__}: {exc}",
                credits_remaining=states_budget.remaining(),
            )

        except (RedisError, RuntimeError, KeyError, TypeError, ValueError) as exc:
            return self._failure(
                region=region,
                error=f"{type(exc).__name__}: {exc}",
                credits_remaining=states_budget.remaining(),
            )

        remaining = _parse_int_header(
            response.headers.get("X-Rate-Limit-Remaining")
        )

        retry_after = _parse_int_header(
            response.headers.get(
                "X-Rate-Limit-Retry-After-Seconds"
            )
        )

        # Server quota information is more authoritative than our local
        # estimate whenever it is available.
        if remaining is not None:
            states_budget.sync_with_server(remaining)
        elif response.is_success:
            states_budget.record_local_spend(cost)

        if response.status_code == 429:
            return self._failure(
                region=region,
                error="OpenSky rate limit exceeded",
                credits_remaining=remaining,
                retry_after_s=retry_after,
                status_code=429,
            )

        # If the refresh-and-retry above also received 401, report it.
        if response.status_code == 401:
            await self.invalidate_token()

            return self._failure(
                region=region,
                error="OpenSky authentication failed after token refresh",
                credits_remaining=remaining,
                status_code=401,
            )

        if not response.is_success:
            return self._failure(
                region=region,
                error=f"OpenSky returned HTTP {response.status_code}",
                credits_remaining=remaining,
                retry_after_s=retry_after,
                status_code=response.status_code,
            )

        try:
            payload: dict[str, Any] = response.json()

            parsed = parse_states(
                payload=payload,
                region=region.name,
            )

        except (ValueError, TypeError, KeyError) as exc:
            return self._failure(
                region=region,
                error=(
                    "invalid OpenSky response: "
                    f"{type(exc).__name__}: {exc}"
                ),
                credits_remaining=remaining,
                status_code=response.status_code,
            )

        return FetchResult(
            region=region.name,
            ok=True,
            parsed=parsed,
            credits_remaining=remaining,
            retry_after_s=None,
            status_code=response.status_code,
            error=None,
        )

    async def get_token(self) -> str:
        redis = get_redis()

        # Fast path: token already exists.
        cached = await redis.get(TOKEN_KEY)
        if cached:
            return cached

        lock_value = uuid.uuid4().hex

        acquired = await redis.set(
            TOKEN_LOCK_KEY,
            lock_value,
            nx=True,
            ex=TOKEN_LOCK_TTL_S,
        )

        if acquired:
            try:
                # Check again after obtaining the lock.
                # Another worker may have refreshed the token just before
                # we acquired it.
                cached = await redis.get(TOKEN_KEY)
                if cached:
                    return cached

                token, expires_in = await self._fetch_token()

                ttl = max(
                    expires_in - TOKEN_REFRESH_MARGIN_S,
                    60,
                )

                await redis.set(
                    TOKEN_KEY,
                    token,
                    ex=ttl,
                )

                return token

            finally:
                await self._release_token_lock(
                    redis=redis,
                    lock_value=lock_value,
                )

        # Another worker owns the refresh lock.
        # Wait for it to populate the shared token cache.
        elapsed = 0.0

        while elapsed < TOKEN_WAIT_TIMEOUT_S:
            await asyncio.sleep(TOKEN_WAIT_INTERVAL_S)
            elapsed += TOKEN_WAIT_INTERVAL_S

            cached = await redis.get(TOKEN_KEY)

            if cached:
                return cached

        raise RuntimeError(
            "timed out waiting for OpenSky token refresh"
        )

    async def invalidate_token(self) -> None:
        """Remove the cached OpenSky access token."""

        redis = get_redis()
        await redis.delete(TOKEN_KEY)

    async def _fetch_token(self) -> tuple[str, int]:
        """Request a new OAuth2 access token."""

        if (
            not settings.opensky_client_id
            or not settings.opensky_client_secret
        ):
            raise RuntimeError(
                "OpenSky credentials are not configured"
            )

        client, should_close = self._client()

        try:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.opensky_client_id,
                    "client_secret": settings.opensky_client_secret,
                },
            )

            response.raise_for_status()

            payload = response.json()

            token = payload["access_token"]
            expires_in = int(payload["expires_in"])

            if not isinstance(token, str) or not token:
                raise ValueError(
                    "OpenSky token response contained invalid access_token"
                )

            return token, expires_in

        finally:
            if should_close:
                await client.aclose()

    async def _get_states(
        self,
        token: str,
        region: Region,
    ) -> httpx.Response:
        """Request /states/all for one bounding box."""

        client, should_close = self._client()

        try:
            return await client.get(
                STATES_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                },
                params={
                    "lamin": region.lamin,
                    "lomin": region.lomin,
                    "lamax": region.lamax,
                    "lomax": region.lomax,
                    "extended": 1,
                },
            )

        finally:
            if should_close:
                await client.aclose()

    def _client(
        self,
    ) -> tuple[httpx.AsyncClient, bool]:
        """Return an HTTP client and whether this method owns it."""

        if self._external_client is not None:
            return self._external_client, False

        return (
            httpx.AsyncClient(timeout=15.0),
            True,
        )

    @staticmethod
    def _failure(
        region: Region,
        error: str,
        *,
        credits_remaining: int | None = None,
        retry_after_s: int | None = None,
        status_code: int | None = None,
    ) -> FetchResult:
        """Build a consistent failed FetchResult."""

        return FetchResult(
            region=region.name,
            ok=False,
            parsed=None,
            credits_remaining=credits_remaining,
            retry_after_s=retry_after_s,
            status_code=status_code,
            error=error,
        )


def _parse_int_header(value: str | None) -> int | None:
    """Parse integer metadata without breaking ingestion."""

    if value is None:
        return None

    try:
        return int(value)

    except ValueError:
        return None