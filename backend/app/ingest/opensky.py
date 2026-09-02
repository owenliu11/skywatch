from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.ingest.budget import CreditBudget, bbox_cost
from app.redis_client import get_redis

states_budget = CreditBudget(settings.daily_credit_budget)

TOKEN_URL = (
    "https://auth.opensky-network.org/"
    "auth/realms/opensky-network/protocol/openid-connect/token"
)

STATES_URL = "https://opensky-network.org/api/states/all"

TOKEN_KEY = "opensky:access_token"

@dataclass
class OpenSkyResponse:
    data: dict[str, Any]
    remaining_credits: int | None
    retry_after_seconds: int | None

async def fetch_states(
    lamin: float,
    lomin: float,
    lamax: float,
    lomax: float,
) -> OpenSkyResponse:
    cost = bbox_cost(lamin, lomin, lamax, lomax)

    if not states_budget.can_afford(cost):
        raise RuntimeError("OpenSky /states credit budget exhausted")

    token = await get_token()

    headers = {
        "Authorization": f"Bearer {token}",
    }

    params = {
        "lamin": lamin,
        "lomin": lomin,
        "lamax": lamax,
        "lomax": lomax,
        "extended": 1,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            STATES_URL,
            headers=headers,
            params=params,
        )

    remaining_header = response.headers.get("X-Rate-Limit-Remaining")
    retry_after_header = response.headers.get(
        "X-Rate-Limit-Retry-After-Seconds"
    )

    remaining_credits = (
        int(remaining_header)
        if remaining_header is not None
        else None
    )

    retry_after_seconds = (
        int(retry_after_header)
        if retry_after_header is not None
        else None
    )

    if remaining_credits is not None:
        states_budget.sync_with_server(remaining_credits)
    else:
        states_budget.record_local_spend(cost)

    response.raise_for_status()

    return OpenSkyResponse(
        data=response.json(),
        remaining_credits=remaining_credits,
        retry_after_seconds=retry_after_seconds,
    )

async def fetch_token() -> tuple[str, int]:
    if not settings.opensky_client_id or not settings.opensky_client_secret:
        raise RuntimeError("OpenSky credentials are not configured")

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.opensky_client_id,
                "client_secret": settings.opensky_client_secret,
            },
        )

        response.raise_for_status()

        data = response.json()

        return data["access_token"], data["expires_in"]

async def get_token() -> str:
    redis = get_redis()

    cached_token = await redis.get(TOKEN_KEY)

    if cached_token:
        return cached_token

    token, expires_in = await fetch_token()

    refresh_margin = 300
    ttl = max(expires_in - refresh_margin, 60)

    await redis.set(
        TOKEN_KEY,
        token,
        ex=ttl,
    )

    return token